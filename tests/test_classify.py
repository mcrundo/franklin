"""Unit tests for the heuristic chapter classifier."""

from __future__ import annotations

import pytest

from franklin.classify import classify_chapter, classify_chapters
from franklin.schema import ChapterKind, CodeBlock, NormalizedChapter


def _chapter(
    *,
    title: str,
    order: int = 5,
    word_count: int = 4000,
    code_blocks: int = 10,
) -> NormalizedChapter:
    return NormalizedChapter(
        chapter_id=f"ch{order:02d}",
        title=title,
        order=order,
        source_ref=f"OEBPS/ch{order:02d}.xhtml",
        word_count=word_count,
        text="body" * word_count,
        code_blocks=[CodeBlock(language="ruby", code="x = 1") for _ in range(code_blocks)],
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Table of Contents", ChapterKind.FRONT_MATTER),
        ("Copyright", ChapterKind.FRONT_MATTER),
        ("Dedication", ChapterKind.FRONT_MATTER),
        ("Acknowledgments", ChapterKind.FRONT_MATTER),
        ("Acknowledgements", ChapterKind.FRONT_MATTER),
        ("Preface", ChapterKind.INTRODUCTION),
        ("Foreword", ChapterKind.INTRODUCTION),
        ("Introduction", ChapterKind.INTRODUCTION),
        ("Part 1: Exploring Rails and Its Abstractions", ChapterKind.PART_DIVIDER),
        ("Part II: Patterns", ChapterKind.PART_DIVIDER),
        ("Part Three: Putting It Together", ChapterKind.PART_DIVIDER),
        ("Chapter 1: Rails as a Web Application Framework", ChapterKind.CONTENT),
        ("Chapter IV: Anti-Patterns", ChapterKind.CONTENT),
        ("About the Author", ChapterKind.BACK_MATTER),
        ("About the Authors", ChapterKind.BACK_MATTER),
        ("Index", ChapterKind.BACK_MATTER),
        ("Bibliography", ChapterKind.BACK_MATTER),
        ("Glossary", ChapterKind.BACK_MATTER),
        ("Other Books You May Enjoy", ChapterKind.BACK_MATTER),
        ("Colophon", ChapterKind.BACK_MATTER),
    ],
)
def test_strong_title_rules(title: str, expected: ChapterKind) -> None:
    result = classify_chapter(_chapter(title=title), total_chapters=20)
    assert result.kind == expected
    assert result.confidence >= 0.9


def test_short_early_chapter_is_front_matter() -> None:
    result = classify_chapter(
        _chapter(title="Praise for This Book", order=1, word_count=20, code_blocks=0),
        total_chapters=20,
    )
    assert result.kind == ChapterKind.FRONT_MATTER
    assert "structural" in result.reason


def test_short_mid_book_chapter_is_part_divider() -> None:
    result = classify_chapter(
        _chapter(title="Advanced Topics", order=10, word_count=80, code_blocks=0),
        total_chapters=20,
    )
    assert result.kind == ChapterKind.PART_DIVIDER


def test_short_late_chapter_is_back_matter() -> None:
    result = classify_chapter(
        _chapter(title="Closing Remarks", order=19, word_count=50, code_blocks=0),
        total_chapters=20,
    )
    assert result.kind == ChapterKind.BACK_MATTER


def test_late_appendix_without_code_is_back_matter() -> None:
    """Late-position chapter with moderate words but no code reads as appendix."""
    result = classify_chapter(
        _chapter(title="Gems and Patterns", order=20, word_count=634, code_blocks=0),
        total_chapters=21,
    )
    assert result.kind == ChapterKind.BACK_MATTER


def test_technical_chapter_is_content() -> None:
    result = classify_chapter(
        _chapter(
            title="Service Objects and Their Discontents",
            order=7,
            word_count=5500,
            code_blocks=22,
        ),
        total_chapters=20,
    )
    assert result.kind == ChapterKind.CONTENT


def test_classify_chapters_keys_by_id() -> None:
    chapters = [
        _chapter(title="Preface", order=1, word_count=1400, code_blocks=2),
        _chapter(title="Chapter 1: Start Here", order=2, word_count=4000, code_blocks=10),
        _chapter(title="Index", order=3, word_count=500, code_blocks=0),
    ]
    results = classify_chapters(chapters)
    assert set(results) == {"ch01", "ch02", "ch03"}
    assert results["ch01"].kind == ChapterKind.INTRODUCTION
    assert results["ch02"].kind == ChapterKind.CONTENT
    assert results["ch03"].kind == ChapterKind.BACK_MATTER
