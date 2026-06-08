"""Heuristic citation-integrity check for generated reference content.

The reduce stage renders an inline ``_source: chNN §X`` tag from the
deterministic sidecar metadata, but the surrounding prose is written
free-hand by the model. Occasionally the model states a chapter number
in prose ("Chapter 21 closes the book") that contradicts the ``_source``
tags the same file draws from. This is an extraction artifact, not an
emission bug, so it can't be fixed deterministically — only surfaced.

This scanner compares chapter numbers mentioned in prose against the
chapter numbers in the file's own ``_source`` tags and flags any prose
number that no ``_source`` in the file backs. It is intentionally a
per-file heuristic (the ``_source`` tags are the trustworthy anchors),
so it is reported as a *warning*, not a hard failure. Pure Python, no
LLM calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# `_source: ch28 §2` / `_source: ch07 listing 3.1` — the deterministic anchor.
_SOURCE_TAG_RE = re.compile(r"_source:\s*ch0*(\d+)", re.IGNORECASE)

# Prose chapter references: "Chapter 21", "Ch. 3", "chapter 7".
_PROSE_CHAPTER_RE = re.compile(r"\b[Cc]h(?:apter|\.)?\s*0*(\d+)\b")

# Fenced code blocks are stripped before scanning prose so code samples that
# mention "Chapter N" or shell `ch07` tokens don't produce false positives.
_FENCE_RE = re.compile(r"^```")


@dataclass(frozen=True)
class CitationMismatch:
    """A prose chapter reference unsupported by the file's own _source tags."""

    source_file: Path
    line_number: int
    prose_chapter: int
    cited_chapters: tuple[int, ...]
    context: str


def check_citations(plugin_root: Path) -> list[CitationMismatch]:
    """Return prose chapter references contradicted by a file's _source tags.

    A file with no ``_source`` tags is skipped entirely — without anchors
    there's nothing to validate against, and flagging every chapter mention
    in such a file would be noise.
    """
    mismatches: list[CitationMismatch] = []
    for md_file in sorted(plugin_root.rglob("*.md")):
        mismatches.extend(_check_file(md_file))
    return mismatches


def _check_file(md_file: Path) -> list[CitationMismatch]:
    text = md_file.read_text(encoding="utf-8")
    cited = {int(m.group(1)) for m in _SOURCE_TAG_RE.finditer(text)}
    if not cited:
        return []

    mismatches: list[CitationMismatch] = []
    in_fence = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Don't treat the _source tag itself as prose.
        prose = _SOURCE_TAG_RE.sub("", line)
        for match in _PROSE_CHAPTER_RE.finditer(prose):
            chapter = int(match.group(1))
            if chapter not in cited:
                mismatches.append(
                    CitationMismatch(
                        source_file=md_file,
                        line_number=line_number,
                        prose_chapter=chapter,
                        cited_chapters=tuple(sorted(cited)),
                        context=line.strip(),
                    )
                )
    return mismatches
