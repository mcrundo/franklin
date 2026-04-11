"""Tests for the run cost estimator."""

from __future__ import annotations

from datetime import UTC, datetime

from franklin.estimate import estimate_run
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterKind,
    NormalizedChapter,
    TocEntry,
)


def _book(
    n_content: int, words_per_chapter: int = 5000
) -> tuple[BookManifest, list[NormalizedChapter]]:
    toc = [
        TocEntry(
            id=f"ch{i:02d}",
            title=f"Chapter {i}",
            source_ref=f"pp.{i}",
            kind=ChapterKind.CONTENT,
            word_count=words_per_chapter,
        )
        for i in range(1, n_content + 1)
    ]
    chapters = [
        NormalizedChapter(
            chapter_id=f"ch{i:02d}",
            title=f"Chapter {i}",
            order=i,
            source_ref=f"pp.{i}",
            word_count=words_per_chapter,
            text="lorem ipsum " * (words_per_chapter // 2),
        )
        for i in range(1, n_content + 1)
    ]
    book = BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path="/books/x.epub",
            sha256="0" * 64,
            format="epub",
            ingested_at=datetime.now(UTC),
        ),
        metadata=BookMetadata(title="Test Book", authors=["Ada"]),
        structure=BookStructure(toc=toc),
    )
    return book, chapters


def test_estimate_returns_all_stages() -> None:
    book, chapters = _book(n_content=10)
    result = estimate_run(book, chapters)
    stages = {s.stage for s in result.stages}
    assert stages == {"map", "plan", "reduce"}


def test_estimate_includes_cleanup_when_flagged() -> None:
    book, chapters = _book(n_content=5)
    result = estimate_run(book, chapters, include_cleanup=True)
    stages = {s.stage for s in result.stages}
    assert "cleanup" in stages


def test_estimate_scales_with_chapter_count() -> None:
    book_small, c_small = _book(n_content=5)
    book_large, c_large = _book(n_content=30)
    small = estimate_run(book_small, c_small).total_cost_usd
    large = estimate_run(book_large, c_large).total_cost_usd
    assert large > small


def test_estimate_totals_sum_correctly() -> None:
    book, chapters = _book(n_content=8)
    result = estimate_run(book, chapters)
    assert result.total_input_tokens == sum(s.input_tokens for s in result.stages)
    assert result.total_output_tokens == sum(s.output_tokens for s in result.stages)
    assert abs(result.total_cost_usd - sum(s.cost_usd for s in result.stages)) < 1e-9


def test_estimate_map_call_count_matches_content_chapters() -> None:
    book, chapters = _book(n_content=12)
    result = estimate_run(book, chapters)
    map_stage = next(s for s in result.stages if s.stage == "map")
    assert map_stage.calls == 12


def test_estimate_reduce_has_at_least_base_artifacts() -> None:
    book, chapters = _book(n_content=2)
    result = estimate_run(book, chapters)
    reduce_stage = next(s for s in result.stages if s.stage == "reduce")
    assert reduce_stage.calls >= 8


def test_estimate_allowed_ids_narrows_map_calls() -> None:
    """Pick-flow gate narrows the estimate when a user deselects chapters."""
    book, chapters = _book(n_content=10)
    subset = {"ch01", "ch02", "ch03"}
    result = estimate_run(book, chapters, allowed_ids=subset)
    map_stage = next(s for s in result.stages if s.stage == "map")
    assert map_stage.calls == 3
    assert result.content_chapters == 3


def test_estimate_allowed_ids_reduces_cost() -> None:
    book, chapters = _book(n_content=20)
    full = estimate_run(book, chapters).total_cost_usd
    narrowed = estimate_run(book, chapters, allowed_ids={"ch01", "ch02"}).total_cost_usd
    assert narrowed < full


def test_estimate_exposes_cost_range() -> None:
    """Displayed range: pessimistic point estimate is the high end, low is a
    discounted multiple representing cache savings and output slack."""
    book, chapters = _book(n_content=10)
    result = estimate_run(book, chapters)
    assert result.total_cost_low_usd < result.total_cost_usd
    assert result.total_cost_low_usd > 0
