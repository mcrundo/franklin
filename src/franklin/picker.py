"""Scan directories for book files and cross-reference against run state.

``franklin pick`` walks one or more user-provided directories, finds every
matching book file (``.epub`` by default; ``.pdf`` opt-in via ``--pdf``),
and annotates each with whether a run directory already exists — so the
picker shows "already processed" vs "new".

The discovery logic is pure so the CLI wrapper is a thin renderer: all
the filtering, dedup, and run-state cross-reference lives here and is
testable without touching typer or Rich.
"""

from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from franklin.checkpoint import RunSummary, slugify, summarize_run

DEFAULT_FORMATS: tuple[str, ...] = (".epub",)
ALL_FORMATS: tuple[str, ...] = (".epub", ".pdf")

_BOOKS_DIR_ENV = "FRANKLIN_BOOKS_DIR"


@dataclass(frozen=True)
class BookCandidate:
    """One candidate file surfaced by the picker."""

    path: Path
    size_bytes: int
    run_slug: str
    existing_run: RunSummary | None
    title: str | None = None
    author: str | None = None
    year: str | None = None

    @property
    def is_processed(self) -> bool:
        return self.existing_run is not None

    @property
    def display_name(self) -> str:
        return self.title or self.path.stem

    @property
    def extension(self) -> str:
        return self.path.suffix.lower().lstrip(".")


def default_search_dirs() -> list[Path]:
    """Return the directories ``franklin pick`` scans when no --dir is given.

    Resolution order:

    1. ``FRANKLIN_BOOKS_DIR`` environment variable — colon-separated list
       of paths, mirroring how ``$PATH`` works. Useful when the user has a
       dedicated library folder outside the usual homedir locations.
    2. Fallback list of common locations that exist on the user's system:
       ``~/Books``, ``~/Downloads``, ``~/Documents``. Missing entries are
       silently dropped so the defaults work on fresh machines.
    """
    env = os.environ.get(_BOOKS_DIR_ENV, "").strip()
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]

    candidates = [
        Path.home() / "Books",
        Path.home() / "Media",
        Path.home() / "Downloads",
        Path.home() / "Documents",
    ]
    return [p for p in candidates if p.exists() and p.is_dir()]


def discover_books(
    search_dirs: list[Path],
    *,
    runs_base: Path,
    recursive: bool = True,
    max_results: int = 200,
    formats: tuple[str, ...] = DEFAULT_FORMATS,
    query: str | None = None,
) -> list[BookCandidate]:
    """Find book files under any of ``search_dirs`` and annotate run state.

    Every matching file (by extension in ``formats``, filtered by
    ``query`` if given) is returned as a ``BookCandidate``. Results are
    sorted newest-first by mtime, deduped across directories by resolved
    absolute path, and capped at ``max_results``.

    ``formats`` defaults to .epub only because PDFs require the Tier 4
    cleanup pass to hit parity and tend to pollute picker listings
    otherwise. Pass ``ALL_FORMATS`` (or the CLI's ``--pdf`` flag) to
    include them.

    ``query`` does a case-insensitive substring match on the filename
    stem — the cheap 80/20 version of "natural search". Most users name
    book files after the title; a richer fuzzy / metadata-based search
    is a future enhancement.
    """
    format_set = tuple(f.lower() for f in formats)
    needle = query.lower().strip() if query else None

    seen: set[Path] = set()
    files: list[Path] = []

    for base in search_dirs:
        if not base.exists() or not base.is_dir():
            continue

        iterator = base.rglob("*") if recursive else base.iterdir()
        for path in iterator:
            if not path.is_file():
                continue
            if path.suffix.lower() not in format_set:
                continue

            try:
                rel = path.relative_to(base)
            except ValueError:
                rel = Path(path.name)
            if any(part.startswith(".") for part in rel.parts):
                continue

            if needle is not None and needle not in path.stem.lower():
                continue

            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            seen.add(resolved)

            files.append(path)

    files.sort(key=_safe_mtime, reverse=True)
    files = files[:max_results]

    candidates: list[BookCandidate] = []
    for path in files:
        slug = slugify(path.stem)
        existing = _load_existing_run(runs_base / slug)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        meta = _read_book_metadata(path)
        candidates.append(
            BookCandidate(
                path=path,
                size_bytes=size,
                run_slug=slug,
                existing_run=existing,
                title=meta.get("title"),
                author=meta.get("author"),
                year=meta.get("year"),
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Metadata extraction for the picker
# ---------------------------------------------------------------------------


def _read_book_metadata(path: Path) -> dict[str, str]:
    """Return title/author/year for a book file, filling gaps from the filename.

    For EPUBs we crack the zip open and read only the OPF metadata block —
    cheap enough to run for every candidate in a scan of hundreds of files,
    and it avoids the full ebooklib parse which loads every chapter. For
    other formats we have no structured metadata, so we parse the filename.

    Always returns a dict (possibly empty); callers treat missing keys as
    "unknown". Any parse error is swallowed — a picker must never crash on
    a corrupt file, it just shows fewer columns for that row.
    """
    epub_meta: dict[str, str] = {}
    if path.suffix.lower() == ".epub":
        try:
            epub_meta = _read_epub_opf_metadata(path)
        except Exception:
            epub_meta = {}

    filename_meta = _parse_filename_metadata(path.stem)
    merged: dict[str, str] = {}
    for key in ("title", "author", "year"):
        value = epub_meta.get(key) or filename_meta.get(key)
        if value:
            merged[key] = value
    return merged


_DC_NS = "{http://purl.org/dc/elements/1.1/}"
_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"


def _read_epub_opf_metadata(path: Path) -> dict[str, str]:
    """Parse dc:title / dc:creator / dc:date from an EPUB's OPF package.

    Only the OPF is read — not the spine, manifest, or any chapter XHTML —
    so this stays fast even over a library of hundreds of books.
    """
    with zipfile.ZipFile(path) as zf:
        try:
            container = zf.read("META-INF/container.xml")
        except KeyError:
            return {}
        root = ET.fromstring(container)
        rootfile = root.find(f".//{_CONTAINER_NS}rootfile")
        if rootfile is None:
            return {}
        opf_path = rootfile.get("full-path")
        if not opf_path:
            return {}
        try:
            opf_bytes = zf.read(opf_path)
        except KeyError:
            return {}

    opf_root = ET.fromstring(opf_bytes)
    out: dict[str, str] = {}

    title_el = opf_root.find(f".//{_DC_NS}title")
    if title_el is not None and (title_el.text or "").strip():
        out["title"] = (title_el.text or "").strip()

    creator_el = opf_root.find(f".//{_DC_NS}creator")
    if creator_el is not None and (creator_el.text or "").strip():
        out["author"] = (creator_el.text or "").strip()

    date_el = opf_root.find(f".//{_DC_NS}date")
    if date_el is not None:
        year = _extract_year(date_el.text or "")
        if year:
            out["year"] = year

    return out


_FILENAME_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_YEAR_IN_PARENS_RE = re.compile(r"[\(\[\{]\s*((?:19|20)\d{2})\s*[\)\]\}]")


def _parse_filename_metadata(stem: str) -> dict[str, str]:
    """Extract author/title/year from common filename patterns.

    Handles the two conventions that cover most real-world epub libraries:

        ``Author - Title`` / ``Author - Title (Year)``
        ``Title (Year)`` (no author)

    When an author is present it is distinguished from a title by position
    (left of the first `` - ``). Years are pulled out of parentheses first,
    then any loose 4-digit year as a fallback. This is intentionally
    conservative — if the filename doesn't look like one of these shapes
    we return the whole stem as the title and leave author/year empty.
    """
    out: dict[str, str] = {}
    working = stem.strip()

    year_match = _YEAR_IN_PARENS_RE.search(working)
    if year_match:
        out["year"] = year_match.group(1)
        working = (working[: year_match.start()] + working[year_match.end() :]).strip()
    else:
        loose = _FILENAME_YEAR_RE.search(working)
        if loose:
            out["year"] = loose.group(0)

    # Strip any trailing empty parens left after year removal.
    working = re.sub(r"[\(\[\{]\s*[\)\]\}]", "", working).strip()
    working = re.sub(r"\s{2,}", " ", working)

    if " - " in working:
        left, _, right = working.partition(" - ")
        left = left.strip()
        right = right.strip()
        if left and right:
            out["author"] = left
            out["title"] = right
        elif right:
            out["title"] = right
        elif left:
            out["title"] = left
    elif working:
        out["title"] = working

    return out


def _extract_year(value: str) -> str | None:
    match = _FILENAME_YEAR_RE.search(value)
    return match.group(0) if match else None


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _load_existing_run(run_dir: Path) -> RunSummary | None:
    if not run_dir.exists() or not run_dir.is_dir():
        return None
    try:
        return summarize_run(run_dir)
    except Exception:
        return None
