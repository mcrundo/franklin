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

from typing import Any

from pydantic import ValidationError

from franklin.llm import call_tool, make_client, render_prompt
from franklin.schema import (
    BookManifest,
    ChapterExtraction,
    ChapterSidecar,
    CodeBlock,
    NormalizedChapter,
)

DEFAULT_MODEL = "claude-sonnet-4-6"

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
    max_tokens: int = 16_000,
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

    result = call_tool(
        client=llm,
        model=model,
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
        tool_schema=tool_schema,
        max_tokens=max_tokens,
    )

    try:
        extraction = ChapterExtraction.model_validate(result.input)
    except ValidationError as exc:
        raise RuntimeError(
            f"extractor returned invalid payload for {chapter.chapter_id}: {exc}"
        ) from exc

    sidecar = ChapterSidecar.from_extraction(chapter, extraction)
    return sidecar, result.input_tokens, result.output_tokens


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
        parts.append(
            f"### code-block-{index} ({language})\n\n"
            f"```{fence_lang}\n{block.code}\n```"
        )
    return "\n\n".join(parts)
