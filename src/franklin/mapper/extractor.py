"""Per-chapter structured extraction.

Given a BookManifest and a NormalizedChapter, build the extraction prompt,
force Claude to call save_chapter_extraction via tool-use, validate the
response against ChapterExtraction, and merge it with ingest metadata
into a full ChapterSidecar.

The extractor is the only place in Franklin that knows the shape of the
extraction prompt, the tool schema, and how to merge the LLM output with
ingest data. Keep the responsibilities here tight.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from franklin.llm import (
    call_tool,
    call_tool_async,
    make_async_client,
    make_client,
    render_prompt,
    validate_with_extra_recovery,
)
from franklin.llm.client import DEFAULT_MAX_TOKENS
from franklin.llm.models import MAP_MODEL
from franklin.schema import (
    BookManifest,
    ChapterExtraction,
    ChapterSidecar,
    CodeBlock,
    NormalizedChapter,
)

DEFAULT_MODEL = MAP_MODEL

# When the LLM returns a payload that fails ChapterExtraction validation
# (most commonly a stringified JSON list it couldn't stuff into a real
# array), retry the whole tool call a few times before giving up. Non-zero
# temperature plus a fresh request usually gets us a clean payload on the
# second or third try, saving the user from re-running the whole stage.
_MAX_VALIDATION_ATTEMPTS = 3

logger = logging.getLogger(__name__)

_TOOL_NAME = "save_chapter_extraction"
_TOOL_DESCRIPTION = (
    "Persist the structured extraction of a single book chapter. Call this "
    "tool exactly once per chapter with the extracted concepts, principles, "
    "rules, anti-patterns, code examples, decision rules, actionable "
    "workflows, terminology, and cross-references."
)

_SYSTEM_PROMPT = (
    "You extract structured knowledge from technical book chapters for use in "
    "Claude Code plugins. You are rigorous about citing source locations, "
    "preserving the author's voice, and not inventing content the chapter "
    "does not contain. You always respond by calling the tool you are given, "
    "never with prose."
)


def extract_chapter(
    book: BookManifest,
    chapter: NormalizedChapter,
    *,
    model: str = DEFAULT_MODEL,
    client: Any | None = None,
    max_tokens: int | None = None,
) -> tuple[ChapterSidecar, int, int]:
    """Run the map stage against one chapter.

    Returns the merged ChapterSidecar along with input and output token
    counts, so callers can log cost per call.

    Raises RuntimeError on tool-use failure and ValidationError if the
    returned payload does not match ChapterExtraction.
    """
    llm = client if client is not None else make_client()
    user_prompt = build_user_prompt(book, chapter)
    tool_schema = build_tool_schema()

    tokens_in = 0
    tokens_out = 0
    last_error: ValidationError | None = None
    for attempt in range(1, _MAX_VALIDATION_ATTEMPTS + 1):
        result = call_tool(
            client=llm,
            model=model,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            tool_name=_TOOL_NAME,
            tool_description=_TOOL_DESCRIPTION,
            tool_schema=tool_schema,
            max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
        )
        tokens_in += result.input_tokens
        tokens_out += result.output_tokens
        try:
            extraction = validate_with_extra_recovery(
                ChapterExtraction,
                result.input,
                label=f"mapper:{chapter.chapter_id}",
            )
        except ValidationError as exc:
            last_error = exc
            logger.warning(
                "mapper:%s: attempt %d/%d returned invalid payload, retrying: %s",
                chapter.chapter_id,
                attempt,
                _MAX_VALIDATION_ATTEMPTS,
                _summarize_error(exc),
            )
            continue
        sidecar = ChapterSidecar.from_extraction(chapter, extraction)
        return sidecar, tokens_in, tokens_out

    assert last_error is not None
    raise RuntimeError(
        f"extractor returned invalid payload for {chapter.chapter_id} "
        f"after {_MAX_VALIDATION_ATTEMPTS} attempts: {last_error}"
    ) from last_error


async def extract_chapter_async(
    book: BookManifest,
    chapter: NormalizedChapter,
    *,
    model: str = DEFAULT_MODEL,
    client: Any | None = None,
    max_tokens: int | None = None,
) -> tuple[ChapterSidecar, int, int]:
    """Async counterpart of ``extract_chapter`` for concurrent mapping."""
    llm = client if client is not None else make_async_client()
    user_prompt = build_user_prompt(book, chapter)
    tool_schema = build_tool_schema()

    tokens_in = 0
    tokens_out = 0
    last_error: ValidationError | None = None
    for attempt in range(1, _MAX_VALIDATION_ATTEMPTS + 1):
        result = await call_tool_async(
            client=llm,
            model=model,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            tool_name=_TOOL_NAME,
            tool_description=_TOOL_DESCRIPTION,
            tool_schema=tool_schema,
            max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
        )
        tokens_in += result.input_tokens
        tokens_out += result.output_tokens
        try:
            extraction = validate_with_extra_recovery(
                ChapterExtraction,
                result.input,
                label=f"mapper:{chapter.chapter_id}",
            )
        except ValidationError as exc:
            last_error = exc
            logger.warning(
                "mapper:%s: attempt %d/%d returned invalid payload, retrying: %s",
                chapter.chapter_id,
                attempt,
                _MAX_VALIDATION_ATTEMPTS,
                _summarize_error(exc),
            )
            continue
        sidecar = ChapterSidecar.from_extraction(chapter, extraction)
        return sidecar, tokens_in, tokens_out

    assert last_error is not None
    raise RuntimeError(
        f"extractor returned invalid payload for {chapter.chapter_id} "
        f"after {_MAX_VALIDATION_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def _summarize_error(exc: ValidationError) -> str:
    """One-line summary of a ValidationError for log readability."""
    errors = exc.errors()
    if not errors:
        return "unknown validation error"
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ())) or "<root>"
    return f"{first.get('type', 'error')} at {loc} ({len(errors)} total)"


def build_user_prompt(book: BookManifest, chapter: NormalizedChapter) -> str:
    """Render the extraction prompt for one chapter."""
    return render_prompt(
        "extract_chapter",
        book_title=book.metadata.title,
        book_authors=", ".join(book.metadata.authors) or "—",
        chapter_title=chapter.title,
        chapter_id=chapter.chapter_id,
        word_count=str(chapter.word_count),
        chapter_text=chapter.text,
        code_blocks=format_code_blocks(chapter.code_blocks),
    )


def build_tool_schema() -> dict[str, Any]:
    """Pydantic-derived JSON schema for the save_chapter_extraction tool."""
    return ChapterExtraction.model_json_schema()


def format_code_blocks(blocks: list[CodeBlock]) -> str:
    """Format chapter code blocks as a markdown section for the prompt."""
    if not blocks:
        return "_(no code blocks extracted from this chapter)_"

    parts: list[str] = []
    for index, block in enumerate(blocks, start=1):
        language = block.language or "text"
        fence_lang = block.language or ""
        parts.append(f"### code-block-{index} ({language})\n\n```{fence_lang}\n{block.code}\n```")
    return "\n\n".join(parts)
