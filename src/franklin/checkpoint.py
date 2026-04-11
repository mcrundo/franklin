"""Run directory layout and load/save helpers.

A run directory is the unit of work in Franklin. Each book gets its own.
Every pipeline stage reads and writes through this module so stages never
share in-memory state — a failed run resumes purely from disk.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
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


# ---------------------------------------------------------------------------
# Run history summaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    """One line in ``franklin runs list``.

    Every field is optional past ``slug`` and ``path`` because run dirs
    on disk may be partial (crashed after ingest, never reduced, etc).
    Readers should treat missing fields as "not yet reached" rather than
    an error — the whole point of this surface is to show partial state.
    """

    slug: str
    path: Path
    title: str | None
    authors: list[str]
    ingested_at: datetime | None
    stages_done: list[str]
    last_stage: str | None
    artifact_count: int | None
    grade_letter: str | None
    grade_score: float | None


def summarize_run(run_dir: Path) -> RunSummary:
    """Build a lightweight summary of a run directory for listings.

    Reads book.json, plan.json, and metrics.json if they exist. Never
    raises on missing or corrupt files — a partial run dir still yields
    a RunSummary with the fields it has.
    """
    slug = run_dir.name
    title: str | None = None
    authors: list[str] = []
    ingested_at: datetime | None = None
    artifact_count: int | None = None
    grade_letter: str | None = None
    grade_score: float | None = None

    run = RunDirectory(run_dir)

    if run.book_json.exists():
        try:
            book = run.load_book()
            title = book.metadata.title
            authors = list(book.metadata.authors)
            ingested_at = book.source.ingested_at
        except Exception:
            pass

    if run.plan_json.exists():
        try:
            plan = run.load_plan()
            artifact_count = len(plan.artifacts)
        except Exception:
            pass

    metrics_path = run_dir / "metrics.json"
    if metrics_path.exists():
        try:
            data = json.loads(metrics_path.read_text())
            grade_letter = data.get("letter")
            grade_score = data.get("composite_score")
        except Exception:
            pass

    stages_done = _infer_stages_done(run)
    last_stage = stages_done[-1] if stages_done else None

    return RunSummary(
        slug=slug,
        path=run_dir,
        title=title,
        authors=authors,
        ingested_at=ingested_at,
        stages_done=stages_done,
        last_stage=last_stage,
        artifact_count=artifact_count,
        grade_letter=grade_letter,
        grade_score=grade_score,
    )


def _infer_stages_done(run: RunDirectory) -> list[str]:
    """Deduce which pipeline stages have completed based on disk artifacts."""
    done: list[str] = []
    if run.book_json.exists() and any(run.raw_dir.glob("*.json")):
        done.append("ingest")
    if run.chapters_dir.exists() and any(run.chapters_dir.glob("*.json")):
        done.append("map")
    if run.plan_json.exists():
        done.append("plan")
    if run.output_dir.exists():
        plan_name_dirs = [p for p in run.output_dir.iterdir() if p.is_dir()]
        if plan_name_dirs and any(p.rglob("*.md") for p in plan_name_dirs):
            done.append("reduce")
            if any((p / ".claude-plugin" / "plugin.json").exists() for p in plan_name_dirs):
                done.append("assemble")
    return done


def list_runs(base: Path) -> list[RunSummary]:
    """Return a summary for every run directory under ``base``.

    Sorted by ``ingested_at`` descending (newest first); runs with no
    known ingest timestamp sink to the bottom but keep their relative
    filesystem order.
    """
    if not base.exists() or not base.is_dir():
        return []
    summaries = [
        summarize_run(child)
        for child in sorted(base.iterdir())
        if child.is_dir() and not child.name.startswith(".")
    ]

    def _sort_key(s: RunSummary) -> tuple[bool, float]:
        ts = s.ingested_at.timestamp() if s.ingested_at else 0.0
        return (s.ingested_at is None, -ts)

    summaries.sort(key=_sort_key)
    return summaries


def slugify(value: str) -> str:
    """Make a filesystem-safe slug from a book title or filename."""
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "book"
