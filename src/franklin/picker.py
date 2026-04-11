"""Scan a directory for book files and cross-reference against run state.

``franklin pick`` walks a user-provided directory (default ``~/Downloads``
plus a couple of common Books folders), finds every ``.epub`` and ``.pdf``,
and annotates each file with whether a run directory already exists for
it — so the picker can show "already processed" vs "new".

Kept pure so the CLI wrapper is a thin renderer: the discovery logic is
testable without touching typer or Rich, and future GUIs can reuse it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from franklin.checkpoint import RunSummary, slugify, summarize_run

_BOOK_EXTENSIONS: tuple[str, ...] = (".epub", ".pdf")


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


def discover_books(
    search_dir: Path,
    *,
    runs_base: Path,
    recursive: bool = True,
    max_results: int = 200,
) -> list[BookCandidate]:
    """Find every .epub and .pdf under ``search_dir`` and annotate run state.

    Results are sorted by newest first (by mtime) and capped at
    ``max_results`` so the picker stays snappy even over large book
    libraries. Hidden files and dotdirs are skipped.
    """
    if not search_dir.exists() or not search_dir.is_dir():
        return []

    files: list[Path] = []
    iterator = search_dir.rglob("*") if recursive else search_dir.iterdir()
    for path in iterator:
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(search_dir).parts):
            continue
        if path.suffix.lower() in _BOOK_EXTENSIONS:
            files.append(path)

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
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


def _load_existing_run(run_dir: Path) -> RunSummary | None:
    if not run_dir.exists() or not run_dir.is_dir():
        return None
    try:
        return summarize_run(run_dir)
    except Exception:
        return None
