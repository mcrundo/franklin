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
from dataclasses import dataclass
from pathlib import Path

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

    @property
    def is_processed(self) -> bool:
        return self.existing_run is not None

    @property
    def display_name(self) -> str:
        return self.path.stem

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
        candidates.append(
            BookCandidate(
                path=path,
                size_bytes=size,
                run_slug=slug,
                existing_run=existing,
            )
        )
    return candidates


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
