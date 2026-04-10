"""Run directory layout and load/save helpers.

A run directory is the unit of work in Franklin. Each book gets its own.
Every pipeline stage reads and writes through this module so stages never
share in-memory state — a failed run resumes purely from disk.
"""

from __future__ import annotations

import re
from pathlib import Path

from franklin.schema import (
    BookManifest,
    ChapterSidecar,
    NormalizedChapter,
    PlanManifest,
    dump_json,
    parse_json,
)


class RunDirectory:
    """Filesystem layout for a single book's pipeline run."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_book(cls, base: Path, slug: str) -> RunDirectory:
        return cls(base / slug)

    # ---- path helpers ----

    @property
    def book_json(self) -> Path:
        return self.root / "book.json"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def chapters_dir(self) -> Path:
        return self.root / "chapters"

    @property
    def plan_json(self) -> Path:
        return self.root / "plan.json"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    def raw_chapter_path(self, chapter_id: str) -> Path:
        return self.raw_dir / f"{chapter_id}.json"

    def sidecar_path(self, chapter_id: str) -> Path:
        return self.chapters_dir / f"{chapter_id}.json"

    # ---- create ----

    def ensure(self) -> None:
        """Create the directory skeleton if it doesn't exist."""
        for d in (self.root, self.raw_dir, self.chapters_dir, self.output_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- book manifest ----

    def save_book(self, book: BookManifest) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.book_json.write_text(dump_json(book))

    def load_book(self) -> BookManifest:
        return parse_json(BookManifest, self.book_json.read_text())

    # ---- raw chapters ----

    def save_raw_chapter(self, chapter: NormalizedChapter) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.raw_chapter_path(chapter.chapter_id).write_text(dump_json(chapter))

    def load_raw_chapter(self, chapter_id: str) -> NormalizedChapter:
        return parse_json(NormalizedChapter, self.raw_chapter_path(chapter_id).read_text())

    def list_raw_chapters(self) -> list[str]:
        if not self.raw_dir.exists():
            return []
        return sorted(p.stem for p in self.raw_dir.glob("*.json"))

    # ---- sidecars ----

    def save_sidecar(self, sidecar: ChapterSidecar) -> None:
        self.chapters_dir.mkdir(parents=True, exist_ok=True)
        self.sidecar_path(sidecar.chapter_id).write_text(dump_json(sidecar))

    def load_sidecar(self, chapter_id: str) -> ChapterSidecar:
        return parse_json(ChapterSidecar, self.sidecar_path(chapter_id).read_text())

    # ---- plan ----

    def save_plan(self, plan: PlanManifest) -> None:
        self.plan_json.write_text(dump_json(plan))

    def load_plan(self) -> PlanManifest:
        return parse_json(PlanManifest, self.plan_json.read_text())


def slugify(value: str) -> str:
    """Make a filesystem-safe slug from a book title or filename."""
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "book"
