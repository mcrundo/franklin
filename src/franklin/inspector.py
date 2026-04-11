"""Post-ingest inspection of a run directory.

Loads `book.json` plus every `raw/chNN.json` file and produces an
`InspectReport` describing the ingest's output plus any anomalies worth
surfacing before the user commits to the expensive downstream stages.
Pure data — no LLM calls, no network, no rendering. The CLI layer turns
an `InspectReport` into a Rich panel or JSON.

Anomaly categories:

- **misclassified** — a chapter classified as `back_matter` with
  substantive prose (>= 1000 words) and low classifier confidence.
  Almost always indicates a real content chapter the position-based
  heuristic mis-flagged.
- **low_words** — a content chapter whose word count is less than 25%
  of the run's average. Usually means the extractor under-counted that
  specific chapter.
- **under_extraction** — a content chapter with zero code blocks in a
  run where most chapters are code-heavy. Usually means the extractor
  failed to identify code on that chapter's pages.
- **spaceless_runs** — the prose contains a token longer than 30
  characters made of word characters. Strong indicator of a
  concatenation artifact like "ButthereisonepartofeveryappthatRails"
  — specific to PDF ingest, where pdfplumber's word-boundary detection
  can fail on tightly-kerned text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median

from franklin.checkpoint import RunDirectory
from franklin.schema import BookManifest, ChapterKind, NormalizedChapter, TocEntry

_CONTENT_KINDS: frozenset[ChapterKind] = frozenset({ChapterKind.CONTENT, ChapterKind.INTRODUCTION})

_LOW_WORDS_RATIO = 0.25
_MISCLASSIFIED_MIN_WORDS = 1000
_MISCLASSIFIED_MAX_CONFIDENCE = 0.85
_UNDER_EXTRACTION_MIN_WORDS = 500
_UNDER_EXTRACTION_MEDIAN_CODE = 10
_SPACELESS_TOKEN_LENGTH = 30
_SPACELESS_PATTERN = re.compile(r"[A-Za-z]{" + str(_SPACELESS_TOKEN_LENGTH) + r",}")


class InspectError(RuntimeError):
    """Raised when a run directory is missing or unreadable."""


@dataclass(frozen=True)
class Anomaly:
    """One concern surfaced by the inspector for a chapter."""

    chapter_id: str
    kind: str
    message: str


@dataclass(frozen=True)
class ChapterInspection:
    """Per-chapter view with classifier metadata and per-chapter anomalies."""

    chapter: NormalizedChapter
    toc_entry: TocEntry
    anomalies: tuple[Anomaly, ...] = field(default_factory=tuple)

    @property
    def longest_code_block(self) -> str | None:
        if not self.chapter.code_blocks:
            return None
        return max(self.chapter.code_blocks, key=lambda cb: len(cb.code)).code


@dataclass(frozen=True)
class InspectReport:
    """Full inspection of a run directory."""

    book: BookManifest
    chapters: tuple[ChapterInspection, ...]
    anomalies: tuple[Anomaly, ...]

    @property
    def total_chapters(self) -> int:
        return len(self.chapters)

    @property
    def content_chapters(self) -> int:
        return sum(1 for ch in self.chapters if ch.toc_entry.kind in _CONTENT_KINDS)

    @property
    def total_words(self) -> int:
        return sum(ch.chapter.word_count for ch in self.chapters)

    @property
    def avg_content_words(self) -> int:
        counts = [
            ch.chapter.word_count for ch in self.chapters if ch.toc_entry.kind in _CONTENT_KINDS
        ]
        return int(mean(counts)) if counts else 0


def inspect_run(run_dir: Path) -> InspectReport:
    """Load a run directory and return its `InspectReport`.

    Raises `InspectError` if `book.json` or the `raw/` directory is
    missing or unreadable. Individual chapters that fail to parse are
    skipped with no error — the report is best-effort across whatever
    the run produced.
    """
    run = RunDirectory(run_dir)
    if not run.book_json.exists():
        raise InspectError(f"no book.json in {run_dir} — run `franklin ingest` first")
    if not run.raw_dir.exists():
        raise InspectError(f"no raw/ directory in {run_dir} — run `franklin ingest` first")

    book = run.load_book()
    toc_by_id: dict[str, TocEntry] = {e.id: e for e in book.structure.toc}

    chapters: list[ChapterInspection] = []
    for chapter_file in sorted(run.raw_dir.glob("*.json")):
        try:
            chapter = run.load_raw_chapter(chapter_file.stem)
        except Exception:
            continue
        toc = toc_by_id.get(chapter.chapter_id)
        if toc is None:
            continue
        chapters.append(ChapterInspection(chapter=chapter, toc_entry=toc, anomalies=()))

    anomalies = _detect_anomalies(chapters)
    chapters_with_anomalies = _attach_per_chapter(chapters, anomalies)

    return InspectReport(
        book=book,
        chapters=tuple(chapters_with_anomalies),
        anomalies=tuple(anomalies),
    )


def _detect_anomalies(
    chapters: list[ChapterInspection],
) -> list[Anomaly]:
    """Scan the loaded chapters and return any anomalies worth surfacing."""
    if not chapters:
        return []

    content_chapters = [c for c in chapters if c.toc_entry.kind in _CONTENT_KINDS]
    if not content_chapters:
        return []

    avg_content_words = mean(c.chapter.word_count for c in content_chapters)
    median_code_blocks = median(len(c.chapter.code_blocks) for c in content_chapters)

    found: list[Anomaly] = []

    for c in chapters:
        cid = c.chapter.chapter_id

        # 1. Substantive back_matter misclassification
        if (
            c.toc_entry.kind == ChapterKind.BACK_MATTER
            and c.chapter.word_count >= _MISCLASSIFIED_MIN_WORDS
            and c.toc_entry.kind_confidence < _MISCLASSIFIED_MAX_CONFIDENCE
        ):
            found.append(
                Anomaly(
                    chapter_id=cid,
                    kind="misclassified",
                    message=(
                        f"classified as back_matter but has "
                        f"{c.chapter.word_count:,} words and low classifier "
                        f"confidence ({c.toc_entry.kind_confidence:.2f}) — "
                        f"likely a real content chapter"
                    ),
                )
            )

        # 2. Content chapter with suspiciously low word count
        if (
            c.toc_entry.kind in _CONTENT_KINDS
            and c.chapter.word_count < avg_content_words * _LOW_WORDS_RATIO
        ):
            found.append(
                Anomaly(
                    chapter_id=cid,
                    kind="low_words",
                    message=(
                        f"{c.chapter.word_count:,} words is far below the "
                        f"content-chapter average of "
                        f"{int(avg_content_words):,} — "
                        f"extractor may have under-counted"
                    ),
                )
            )

        # 3. Code-heavy book with a zero-code chapter
        if (
            c.toc_entry.kind in _CONTENT_KINDS
            and len(c.chapter.code_blocks) == 0
            and median_code_blocks > _UNDER_EXTRACTION_MEDIAN_CODE
            and c.chapter.word_count > _UNDER_EXTRACTION_MIN_WORDS
        ):
            found.append(
                Anomaly(
                    chapter_id=cid,
                    kind="under_extraction",
                    message=(
                        f"zero code blocks in a code-heavy run (median "
                        f"{int(median_code_blocks)} blocks per content "
                        f"chapter) — extractor may have missed code on "
                        f"this chapter's pages"
                    ),
                )
            )

        # 4. Spaceless prose runs (concatenation artifacts)
        long_tokens = _SPACELESS_PATTERN.findall(c.chapter.text)
        if long_tokens:
            sample = long_tokens[0]
            if len(sample) > 40:
                sample = sample[:40] + "..."
            found.append(
                Anomaly(
                    chapter_id=cid,
                    kind="spaceless_runs",
                    message=(
                        f"found {len(long_tokens)} token(s) longer than "
                        f"{_SPACELESS_TOKEN_LENGTH} chars without spaces "
                        f"(first: {sample!r}) — likely concatenation "
                        f"artifact from PDF extraction"
                    ),
                )
            )

    return found


def _attach_per_chapter(
    chapters: list[ChapterInspection], anomalies: list[Anomaly]
) -> list[ChapterInspection]:
    """Return a copy of each chapter inspection with its anomalies attached."""
    grouped: dict[str, list[Anomaly]] = {}
    for anomaly in anomalies:
        grouped.setdefault(anomaly.chapter_id, []).append(anomaly)
    return [
        ChapterInspection(
            chapter=c.chapter,
            toc_entry=c.toc_entry,
            anomalies=tuple(grouped.get(c.chapter.chapter_id, [])),
        )
        for c in chapters
    ]


def report_to_json(report: InspectReport) -> str:
    """Serialize an InspectReport to JSON for --json output."""
    payload = {
        "book": {
            "title": report.book.metadata.title,
            "authors": list(report.book.metadata.authors),
            "format": report.book.source.format,
        },
        "totals": {
            "chapters": report.total_chapters,
            "content_chapters": report.content_chapters,
            "total_words": report.total_words,
            "avg_content_words": report.avg_content_words,
        },
        "chapters": [
            {
                "chapter_id": c.chapter.chapter_id,
                "title": c.chapter.title,
                "kind": c.toc_entry.kind.value,
                "kind_confidence": c.toc_entry.kind_confidence,
                "word_count": c.chapter.word_count,
                "code_block_count": len(c.chapter.code_blocks),
                "anomaly_kinds": [a.kind for a in c.anomalies],
            }
            for c in report.chapters
        ],
        "anomalies": [
            {
                "chapter_id": a.chapter_id,
                "kind": a.kind,
                "message": a.message,
            }
            for a in report.anomalies
        ],
    }
    return json.dumps(payload, indent=2)
