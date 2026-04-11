"""Tests for franklin.inspector (franklin inspect)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from franklin.checkpoint import RunDirectory
from franklin.inspector import (
    InspectError,
    inspect_run,
    report_to_json,
)
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterKind,
    CodeBlock,
    NormalizedChapter,
    TocEntry,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_run(
    tmp_path: Path,
    chapters: list[NormalizedChapter],
    *,
    toc_overrides: dict[str, dict] | None = None,
) -> Path:
    """Write a minimal run directory with book.json + raw chapter files.

    `toc_overrides` maps chapter_id → {kind, kind_confidence, kind_reason}
    to override the default (CONTENT, 0.85, "test default") classification.
    """
    run_dir = tmp_path / "runs" / "test-run"
    run = RunDirectory(run_dir)
    run.ensure()

    overrides = toc_overrides or {}
    toc = []
    for chapter in chapters:
        override = overrides.get(chapter.chapter_id, {})
        toc.append(
            TocEntry(
                id=chapter.chapter_id,
                title=chapter.title,
                level=1,
                word_count=chapter.word_count,
                source_ref=chapter.source_ref,
                kind=override.get("kind", ChapterKind.CONTENT),
                kind_confidence=override.get("kind_confidence", 0.85),
                kind_reason=override.get("kind_reason", "test default"),
            )
        )

    structure = BookStructure(
        toc=toc,
        total_chapters=len(chapters),
        total_words=sum(c.word_count for c in chapters),
        has_code_examples=any(c.code_blocks for c in chapters),
    )
    book = BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path="x.pdf",
            sha256="0" * 64,
            format="pdf",
            ingested_at=datetime.now(UTC),
        ),
        metadata=BookMetadata(title="Test Book", authors=["Test Author"]),
        structure=structure,
    )
    run.save_book(book)
    for chapter in chapters:
        run.save_raw_chapter(chapter)
    return run_dir


def _chapter(
    chapter_id: str,
    *,
    title: str,
    order: int,
    word_count: int = 3000,
    text: str | None = None,
    code_blocks: int = 10,
) -> NormalizedChapter:
    return NormalizedChapter(
        chapter_id=chapter_id,
        title=title,
        order=order,
        source_ref=f"pp. {order}-{order + 5}",
        word_count=word_count,
        text=text or (" ".join(["word"] * word_count)),
        code_blocks=[
            CodeBlock(language="ruby", code=f"def method_{i}; end") for i in range(code_blocks)
        ],
        headings=[title],
    )


# ---------------------------------------------------------------------------
# Basic load + report shape
# ---------------------------------------------------------------------------


def test_inspect_run_produces_summary(tmp_path: Path) -> None:
    run_dir = _make_run(
        tmp_path,
        [
            _chapter("ch01", title="Intro", order=1, word_count=2000),
            _chapter("ch02", title="Patterns", order=2, word_count=4000),
            _chapter("ch03", title="Closing", order=3, word_count=3000),
        ],
    )
    report = inspect_run(run_dir)
    assert report.total_chapters == 3
    assert report.content_chapters == 3
    assert report.total_words == 9000
    assert report.avg_content_words == 3000
    assert report.book.metadata.title == "Test Book"
    assert len(report.chapters) == 3


def test_inspect_run_errors_on_missing_book_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "empty"
    run_dir.mkdir(parents=True)
    with pytest.raises(InspectError, match=r"no book\.json"):
        inspect_run(run_dir)


def test_inspect_run_errors_on_missing_raw_dir(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "partial"
    run_dir.mkdir(parents=True)
    # Write a book.json but no raw/
    run = RunDirectory(run_dir)
    run.save_book(
        BookManifest(
            franklin_version="0.1.0",
            source=BookSource(
                path="x.pdf",
                sha256="0" * 64,
                format="pdf",
                ingested_at=datetime.now(UTC),
            ),
            metadata=BookMetadata(title="Partial", authors=[]),
            structure=BookStructure(),
        )
    )
    with pytest.raises(InspectError, match="no raw/ directory"):
        inspect_run(run_dir)


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


def test_detects_misclassified_back_matter(tmp_path: Path) -> None:
    """A back_matter chapter with > 1000 words and low confidence is flagged."""
    run_dir = _make_run(
        tmp_path,
        [
            _chapter("ch01", title="Chapter 1", order=1, word_count=3000),
            _chapter("ch02", title="Chapter 2", order=2, word_count=3500),
            _chapter(
                "ch03",
                title="Technical Leadership is Critical",
                order=3,
                word_count=1500,
                code_blocks=0,
            ),
        ],
        toc_overrides={
            "ch03": {
                "kind": ChapterKind.BACK_MATTER,
                "kind_confidence": 0.7,
                "kind_reason": "late position, no code",
            }
        },
    )
    report = inspect_run(run_dir)
    kinds = [a.kind for a in report.anomalies]
    assert "misclassified" in kinds
    ch03 = next(c for c in report.chapters if c.chapter.chapter_id == "ch03")
    assert any(a.kind == "misclassified" for a in ch03.anomalies)


def test_does_not_flag_high_confidence_back_matter(tmp_path: Path) -> None:
    """High-confidence back_matter (title-matched) should NOT be flagged."""
    run_dir = _make_run(
        tmp_path,
        [
            _chapter("ch01", title="Chapter 1", order=1, word_count=3000),
            _chapter("ch02", title="Chapter 2", order=2, word_count=3500),
            _chapter("ch03", title="Index", order=3, word_count=1500, code_blocks=0),
        ],
        toc_overrides={
            "ch03": {
                "kind": ChapterKind.BACK_MATTER,
                "kind_confidence": 0.95,
                "kind_reason": "title matches 'index'",
            }
        },
    )
    report = inspect_run(run_dir)
    assert not any(a.kind == "misclassified" for a in report.anomalies)


def test_detects_low_words_chapter(tmp_path: Path) -> None:
    """A content chapter with < 25% of the average word count is flagged."""
    run_dir = _make_run(
        tmp_path,
        [
            _chapter("ch01", title="Chapter 1", order=1, word_count=4000),
            _chapter("ch02", title="Chapter 2", order=2, word_count=4000),
            _chapter("ch03", title="Chapter 3 (short)", order=3, word_count=500),
        ],
    )
    report = inspect_run(run_dir)
    assert any(a.kind == "low_words" and a.chapter_id == "ch03" for a in report.anomalies)


def test_detects_under_extraction_in_code_heavy_book(tmp_path: Path) -> None:
    """A content chapter with 0 code blocks in a code-heavy run is flagged."""
    run_dir = _make_run(
        tmp_path,
        [
            _chapter("ch01", title="Chapter 1", order=1, word_count=3000, code_blocks=20),
            _chapter("ch02", title="Chapter 2", order=2, word_count=3500, code_blocks=25),
            _chapter("ch03", title="Chapter 3", order=3, word_count=3000, code_blocks=18),
            _chapter("ch04", title="Chapter 4", order=4, word_count=2500, code_blocks=0),
        ],
    )
    report = inspect_run(run_dir)
    assert any(a.kind == "under_extraction" and a.chapter_id == "ch04" for a in report.anomalies)


def test_does_not_flag_zero_code_in_prose_book(tmp_path: Path) -> None:
    """A chapter with zero code should NOT be flagged when the book isn't code-heavy."""
    run_dir = _make_run(
        tmp_path,
        [
            _chapter("ch01", title="Chapter 1", order=1, word_count=3000, code_blocks=0),
            _chapter("ch02", title="Chapter 2", order=2, word_count=3500, code_blocks=1),
            _chapter("ch03", title="Chapter 3", order=3, word_count=3000, code_blocks=0),
        ],
    )
    report = inspect_run(run_dir)
    assert not any(a.kind == "under_extraction" for a in report.anomalies)


def test_detects_spaceless_runs(tmp_path: Path) -> None:
    """Text with a 30+ char token is flagged as a concatenation artifact."""
    bad_text = (
        "This is a normal sentence. "
        "ButthereisonepartofeveryappthatRailsdoesnthaveaclearanswerfor. "
        "Back to normal prose here."
    )
    run_dir = _make_run(
        tmp_path,
        [
            _chapter("ch01", title="Chapter 1", order=1, word_count=3000),
            _chapter("ch02", title="Chapter 2", order=2, text=bad_text, word_count=20),
        ],
    )
    report = inspect_run(run_dir)
    spaceless = [a for a in report.anomalies if a.kind == "spaceless_runs"]
    assert any(a.chapter_id == "ch02" for a in spaceless)


def test_clean_run_has_no_anomalies(tmp_path: Path) -> None:
    run_dir = _make_run(
        tmp_path,
        [
            _chapter("ch01", title="Chapter 1", order=1, word_count=3000, code_blocks=5),
            _chapter("ch02", title="Chapter 2", order=2, word_count=3500, code_blocks=5),
            _chapter("ch03", title="Chapter 3", order=3, word_count=3200, code_blocks=5),
        ],
    )
    report = inspect_run(run_dir)
    assert report.anomalies == ()


# ---------------------------------------------------------------------------
# JSON output shape
# ---------------------------------------------------------------------------


def test_report_to_json_shape(tmp_path: Path) -> None:
    run_dir = _make_run(
        tmp_path,
        [
            _chapter("ch01", title="Chapter 1", order=1, word_count=3000),
            _chapter("ch02", title="Chapter 2", order=2, word_count=3500),
        ],
    )
    report = inspect_run(run_dir)
    payload = json.loads(report_to_json(report))

    assert payload["book"]["title"] == "Test Book"
    assert payload["book"]["format"] == "pdf"
    assert payload["totals"]["chapters"] == 2
    assert payload["totals"]["content_chapters"] == 2
    assert payload["totals"]["total_words"] == 6500
    assert len(payload["chapters"]) == 2
    assert payload["chapters"][0]["chapter_id"] == "ch01"
    assert "kind" in payload["chapters"][0]
    assert "anomaly_kinds" in payload["chapters"][0]
    assert isinstance(payload["anomalies"], list)


# ---------------------------------------------------------------------------
# ChapterInspection helper
# ---------------------------------------------------------------------------


def test_longest_code_block_returns_largest(tmp_path: Path) -> None:
    small = CodeBlock(language="ruby", code="x")
    medium = CodeBlock(language="ruby", code="hello world")
    large = CodeBlock(language="ruby", code="class Big\n  def method; end\nend")

    chapter = NormalizedChapter(
        chapter_id="ch01",
        title="Chapter",
        order=1,
        source_ref="pp. 1-5",
        word_count=100,
        text="body",
        code_blocks=[small, medium, large],
    )
    run_dir = _make_run(tmp_path, [chapter])
    report = inspect_run(run_dir)
    inspection = report.chapters[0]
    assert inspection.longest_code_block == large.code


def test_longest_code_block_none_when_no_code(tmp_path: Path) -> None:
    chapter = _chapter("ch01", title="Prose", order=1, word_count=2000, code_blocks=0)
    run_dir = _make_run(tmp_path, [chapter])
    report = inspect_run(run_dir)
    inspection = report.chapters[0]
    assert inspection.longest_code_block is None
