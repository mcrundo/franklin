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

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from franklin.llm import (
    call_tool,
    call_tool_async,
    make_async_client,
    make_client,
    render_prompt,
)
from franklin.llm.client import DEFAULT_MAX_TOKENS
from franklin.llm.models import CLEANUP_MODEL
from franklin.schema import NormalizedChapter

DEFAULT_CLEANUP_CONCURRENCY = 8
DEFAULT_MODEL = CLEANUP_MODEL

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


def _render_user_prompt(chapter: NormalizedChapter) -> str:
    return render_prompt(
        "clean_chapter",
        chapter_title=chapter.title,
        chapter_id=chapter.chapter_id,
        word_count=str(chapter.word_count),
        chapter_text=chapter.text,
    )


def _parse_cleanup_result(
    chapter: NormalizedChapter, raw_input: dict[str, Any]
) -> NormalizedChapter:
    try:
        payload = _CleanupPayload.model_validate(raw_input)
    except ValidationError as exc:
        raise RuntimeError(
            f"cleanup returned invalid payload for {chapter.chapter_id}: {exc}"
        ) from exc

    cleaned_text = payload.cleaned_text.strip()
    if not cleaned_text:
        raise RuntimeError(f"cleanup returned empty text for {chapter.chapter_id}")

    return NormalizedChapter(
        chapter_id=chapter.chapter_id,
        title=chapter.title,
        order=chapter.order,
        source_ref=chapter.source_ref,
        word_count=len(cleaned_text.split()),
        text=cleaned_text,
        code_blocks=chapter.code_blocks,
        headings=chapter.headings,
    )


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
    result = call_tool(
        client=llm,
        model=model,
        system=_SYSTEM_PROMPT,
        user=_render_user_prompt(chapter),
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
        tool_schema=_CleanupPayload.model_json_schema(),
        max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
    )
    cleaned = _parse_cleanup_result(chapter, result.input)
    return cleaned, result.input_tokens, result.output_tokens


async def clean_chapter_async(
    chapter: NormalizedChapter,
    *,
    client: Any,
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
) -> tuple[NormalizedChapter, int, int]:
    """Async version of ``clean_chapter`` for bounded-concurrency use.

    Requires an AsyncAnthropic-compatible client — callers should share
    a single client across all concurrent calls in a batch.
    """
    result = await call_tool_async(
        client=client,
        model=model,
        system=_SYSTEM_PROMPT,
        user=_render_user_prompt(chapter),
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
        tool_schema=_CleanupPayload.model_json_schema(),
        max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
    )
    cleaned = _parse_cleanup_result(chapter, result.input)
    return cleaned, result.input_tokens, result.output_tokens


def clean_chapters(
    chapters: list[NormalizedChapter],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    on_progress: Callable[[NormalizedChapter], None] | None = None,
    on_failure: Callable[[NormalizedChapter, Exception], None] | None = None,
) -> tuple[list[NormalizedChapter], int, int, list[str]]:
    """Clean every chapter in a list via ``clean_chapter`` (sequential).

    Kept for tests and anywhere strict sequential ordering matters. For
    real ingest runs the CLI uses ``clean_chapters_async`` via
    ``asyncio.run`` because sequential cleanup of a 29-chapter book takes
    ~50 minutes wall clock.

    Per-chapter failures are non-fatal: the original Tier 2 chapter is
    kept in place and the failure is reported through ``on_failure``
    (if provided). Returns the cleaned list, total input tokens, total
    output tokens, and the list of chapter ids that failed.
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


async def clean_chapters_async(
    chapters: list[NormalizedChapter],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    concurrency: int = DEFAULT_CLEANUP_CONCURRENCY,
    on_progress: Callable[[NormalizedChapter], None] | None = None,
    on_failure: Callable[[NormalizedChapter, Exception], None] | None = None,
) -> tuple[list[NormalizedChapter], int, int, list[str]]:
    """Clean every chapter concurrently with a bounded semaphore.

    Turns the sequential ~1.5-2 min per chapter cadence into concurrent
    waves of ``concurrency`` calls. For a 29-chapter book at the default
    concurrency=8, wall clock drops from ~50 minutes to ~6 minutes.

    Output ordering matches the input order (same as the sync version).
    Per-chapter failures are non-fatal; the original chapter is kept and
    the failure is reported via ``on_failure``. Total input/output token
    counts and the list of failed chapter ids are returned alongside.

    The client parameter should be an AsyncAnthropic instance (or a
    compatible fake for tests). Callers share one client across all
    concurrent calls.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    llm = client if client is not None else make_async_client()
    sem = asyncio.Semaphore(concurrency)

    async def one(
        index: int, chapter: NormalizedChapter
    ) -> tuple[int, NormalizedChapter, int, int, Exception | None]:
        async with sem:
            try:
                new_chapter, in_tokens, out_tokens = await clean_chapter_async(
                    chapter, client=llm, model=model
                )
            except Exception as exc:
                if on_failure is not None:
                    on_failure(chapter, exc)
                return index, chapter, 0, 0, exc
            if on_progress is not None:
                on_progress(new_chapter)
            return index, new_chapter, in_tokens, out_tokens, None

    tasks = [one(i, ch) for i, ch in enumerate(chapters)]
    results = await asyncio.gather(*tasks)

    # Results are returned out of order because of concurrency — restore the
    # input order so the caller sees a deterministic result list.
    results.sort(key=lambda r: r[0])

    cleaned: list[NormalizedChapter] = []
    failed_ids: list[str] = []
    total_input = 0
    total_output = 0
    for _idx, chapter_out, in_toks, out_toks, exc in results:
        cleaned.append(chapter_out)
        if exc is not None:
            failed_ids.append(chapter_out.chapter_id)
            continue
        total_input += in_toks
        total_output += out_toks

    return cleaned, total_input, total_output, failed_ids
