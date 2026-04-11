"""Tier 4 PDF cleanup: one LLM call per chapter to fix Tier 2 artifacts.

Only touches prose. Code blocks are passed through unchanged because the
tool schema exposes a single `cleaned_text` field — the LLM never sees or
writes to the code_blocks list, so there is no path by which cleanup can
corrupt code content.

Each cleanup call is a forced tool-use against Claude Sonnet (by default)
returning the full cleaned chapter text. Failures per chapter are
non-fatal: `clean_chapters` returns the original Tier 2 chapter for any
chapter whose cleanup raises, and surfaces the failure via the on_failure
callback so the CLI can warn. This keeps a single flaky chapter from
aborting a whole book's cleanup pass.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from franklin.llm import call_tool, make_client, render_prompt
from franklin.llm.client import DEFAULT_MAX_TOKENS
from franklin.schema import NormalizedChapter

DEFAULT_MODEL = "claude-sonnet-4-6"

_TOOL_NAME = "save_cleaned_chapter"
_TOOL_DESCRIPTION = (
    "Persist the cleaned-up prose of a PDF-extracted chapter. Call this "
    "tool exactly once per chapter with the full cleaned text in the "
    "cleaned_text field. Do not return diffs or summaries — return the "
    "complete chapter text."
)

_SYSTEM_PROMPT = (
    "You clean up prose extracted from PDFs by a heuristic layout-aware "
    "extractor. You fix mechanical artifacts (word concatenations, "
    "hyphen-broken words, stray footnote markers, page furniture) without "
    "paraphrasing, rewriting, or adding content. You preserve the author's "
    "voice exactly. You always respond by calling the tool you are given; "
    "never reply with prose outside the tool call."
)


class _CleanupPayload(BaseModel):
    """Schema for the save_cleaned_chapter tool input."""

    model_config = ConfigDict(extra="forbid")
    cleaned_text: str = Field(description="The full cleaned prose of the chapter")


def clean_chapter(
    chapter: NormalizedChapter,
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
) -> tuple[NormalizedChapter, int, int]:
    """Send one chapter's prose to an LLM for mechanical cleanup.

    Returns a new NormalizedChapter with its text field replaced by the
    cleaned version and its word_count recomputed. All other fields
    (chapter_id, title, order, source_ref, code_blocks, headings) are
    preserved exactly — the LLM never sees them and cannot modify them.

    Raises RuntimeError if the tool-use call fails or the response payload
    doesn't match the expected shape.
    """
    llm = client if client is not None else make_client()
    user_prompt = render_prompt(
        "clean_chapter",
        chapter_title=chapter.title,
        chapter_id=chapter.chapter_id,
        word_count=str(chapter.word_count),
        chapter_text=chapter.text,
    )

    result = call_tool(
        client=llm,
        model=model,
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
        tool_schema=_CleanupPayload.model_json_schema(),
        max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
    )

    try:
        payload = _CleanupPayload.model_validate(result.input)
    except ValidationError as exc:
        raise RuntimeError(
            f"cleanup returned invalid payload for {chapter.chapter_id}: {exc}"
        ) from exc

    cleaned_text = payload.cleaned_text.strip()
    if not cleaned_text:
        raise RuntimeError(f"cleanup returned empty text for {chapter.chapter_id}")

    cleaned_chapter = NormalizedChapter(
        chapter_id=chapter.chapter_id,
        title=chapter.title,
        order=chapter.order,
        source_ref=chapter.source_ref,
        word_count=len(cleaned_text.split()),
        text=cleaned_text,
        code_blocks=chapter.code_blocks,
        headings=chapter.headings,
    )
    return cleaned_chapter, result.input_tokens, result.output_tokens


def clean_chapters(
    chapters: list[NormalizedChapter],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    on_progress: Callable[[NormalizedChapter], None] | None = None,
    on_failure: Callable[[NormalizedChapter, Exception], None] | None = None,
) -> tuple[list[NormalizedChapter], int, int, list[str]]:
    """Clean every chapter in a list via ``clean_chapter``.

    Per-chapter failures are non-fatal: the original Tier 2 chapter is
    kept in place and the failure is reported through ``on_failure`` (if
    provided). Returns the cleaned list, total input tokens, total output
    tokens, and the list of chapter ids that failed.
    """
    llm = client if client is not None else make_client()
    cleaned: list[NormalizedChapter] = []
    failed_ids: list[str] = []
    total_input = 0
    total_output = 0

    for chapter in chapters:
        if on_progress is not None:
            on_progress(chapter)
        try:
            new_chapter, in_tokens, out_tokens = clean_chapter(chapter, client=llm, model=model)
        except Exception as exc:
            if on_failure is not None:
                on_failure(chapter, exc)
            cleaned.append(chapter)
            failed_ids.append(chapter.chapter_id)
            continue
        cleaned.append(new_chapter)
        total_input += in_tokens
        total_output += out_tokens

    return cleaned, total_input, total_output, failed_ids
