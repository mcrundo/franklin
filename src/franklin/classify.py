"""Heuristic chapter classifier.

Given the raw output of the ingest stage, decide for each chapter whether
it's real content or part of the book's scaffolding (front matter, part
dividers, index, etc). Pure Python, no LLM calls — this is an optimization
that keeps later stages from spending tokens on non-content chapters.

Classification happens in two passes per chapter:

1. Strong title rules: patterns like "Preface", "Chapter 3:", "Index" match
   with high confidence (0.95) regardless of length or code density.
2. Structural heuristics: when titles don't match, fall back to word count,
   code block count, and position in the book (0.7-0.75 confidence).

The default is CONTENT at 0.85 confidence — the classifier is biased toward
keeping things rather than skipping them, so a borderline chapter still
goes through the map stage and we only lose tokens, not information.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from franklin.schema import ChapterKind, NormalizedChapter


@dataclass(frozen=True)
class ClassificationResult:
    """Output of classifying a single chapter."""

    kind: ChapterKind
    confidence: float
    reason: str


# Strong title rules. Checked in order — first match wins.
_STRONG_TITLE_RULES: tuple[tuple[re.Pattern[str], ChapterKind, str], ...] = (
    (
        re.compile(r"^\s*table of contents\s*$", re.IGNORECASE),
        ChapterKind.FRONT_MATTER,
        "title matches 'table of contents'",
    ),
    (
        re.compile(r"^\s*copyright\b", re.IGNORECASE),
        ChapterKind.FRONT_MATTER,
        "title matches 'copyright'",
    ),
    (
        re.compile(r"^\s*dedication\b", re.IGNORECASE),
        ChapterKind.FRONT_MATTER,
        "title matches 'dedication'",
    ),
    (
        re.compile(r"^\s*acknowledge?ments?\b", re.IGNORECASE),
        ChapterKind.FRONT_MATTER,
        "title matches 'acknowledgments'",
    ),
    (
        re.compile(r"^\s*(title page|half[- ]title|frontispiece)\b", re.IGNORECASE),
        ChapterKind.FRONT_MATTER,
        "title matches front-matter page",
    ),
    (
        re.compile(r"^\s*about the authors?\b", re.IGNORECASE),
        ChapterKind.BACK_MATTER,
        "title matches 'about the author'",
    ),
    (
        re.compile(r"^\s*index\s*$", re.IGNORECASE),
        ChapterKind.BACK_MATTER,
        "title matches 'index'",
    ),
    (
        re.compile(r"^\s*(bibliography|references)\s*$", re.IGNORECASE),
        ChapterKind.BACK_MATTER,
        "title matches 'bibliography/references'",
    ),
    (
        re.compile(r"^\s*glossary\b", re.IGNORECASE),
        ChapterKind.BACK_MATTER,
        "title matches 'glossary'",
    ),
    (
        re.compile(
            r"^\s*(other books you may enjoy|also by|further reading)\b",
            re.IGNORECASE,
        ),
        ChapterKind.BACK_MATTER,
        "title matches 'other books'",
    ),
    (
        re.compile(r"^\s*colophon\s*$", re.IGNORECASE),
        ChapterKind.BACK_MATTER,
        "title matches 'colophon'",
    ),
    (
        re.compile(r"^\s*(preface|foreword|introduction)\b", re.IGNORECASE),
        ChapterKind.INTRODUCTION,
        "title matches 'preface/foreword/introduction'",
    ),
    (
        re.compile(
            r"^\s*part\s+([ivxlcdm]+|\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
            re.IGNORECASE,
        ),
        ChapterKind.PART_DIVIDER,
        "title matches 'part N'",
    ),
    (
        re.compile(
            r"^\s*chapter\s+(\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
            re.IGNORECASE,
        ),
        ChapterKind.CONTENT,
        "title matches 'chapter N'",
    ),
)


def classify_chapter(
    chapter: NormalizedChapter, *, total_chapters: int
) -> ClassificationResult:
    """Classify one chapter using title rules first, then structural heuristics."""
    for pattern, kind, reason in _STRONG_TITLE_RULES:
        if pattern.match(chapter.title):
            return ClassificationResult(kind=kind, confidence=0.95, reason=reason)

    position = chapter.order / max(total_chapters, 1)

    if chapter.word_count < 200 and not chapter.code_blocks:
        if position < 0.15:
            kind = ChapterKind.FRONT_MATTER
        elif position > 0.85:
            kind = ChapterKind.BACK_MATTER
        else:
            kind = ChapterKind.PART_DIVIDER
        return ClassificationResult(
            kind=kind,
            confidence=0.75,
            reason=(
                f"structural: {chapter.word_count} words, "
                f"no code, position {position:.0%}"
            ),
        )

    if position > 0.85 and chapter.word_count < 1500 and not chapter.code_blocks:
        return ClassificationResult(
            kind=ChapterKind.BACK_MATTER,
            confidence=0.7,
            reason=(
                f"structural: late position {position:.0%}, "
                f"{chapter.word_count} words, no code"
            ),
        )

    return ClassificationResult(
        kind=ChapterKind.CONTENT,
        confidence=0.85,
        reason=(
            f"default: {chapter.word_count} words, "
            f"{len(chapter.code_blocks)} code blocks"
        ),
    )


def classify_chapters(
    chapters: list[NormalizedChapter],
) -> dict[str, ClassificationResult]:
    """Classify every chapter in a book, keyed by chapter_id."""
    total = len(chapters)
    return {c.chapter_id: classify_chapter(c, total_chapters=total) for c in chapters}
