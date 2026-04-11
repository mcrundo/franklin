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

import copy
import logging
from typing import Any

from pydantic import ValidationError

from franklin.llm import call_tool, make_client, render_prompt
from franklin.llm.client import DEFAULT_MAX_TOKENS
from franklin.schema import (
    BookManifest,
    ChapterExtraction,
    ChapterSidecar,
    CodeBlock,
    NormalizedChapter,
)

logger = logging.getLogger(__name__)

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

    extraction = _validate_with_extra_recovery(result.input, chapter.chapter_id)

    sidecar = ChapterSidecar.from_extraction(chapter, extraction)
    return sidecar, result.input_tokens, result.output_tokens


def _validate_with_extra_recovery(payload: Any, chapter_id: str) -> ChapterExtraction:
    """Validate a tool-use payload, recovering from stray extra keys.

    The ChapterExtraction schema (and its nested models) forbid unknown
    fields so the outgoing tool schema sent to Claude says
    ``additionalProperties: false`` — this keeps the model mostly
    on-contract. But models do occasionally slip an extra key onto a
    sub-object (e.g. generalizing ``source_quote`` from ``Concept`` onto
    ``Principle``), and a single stray key shouldn't kill a whole
    chapter's worth of extraction work.

    Strategy: validate strictly once; if the *only* failures are
    ``extra_forbidden`` errors, strip exactly those paths from the
    payload and retry. Any other validation error (missing required
    field, wrong type) still raises. The strip is logged so the drift
    is visible in run logs instead of silently swallowed.
    """
    try:
        return ChapterExtraction.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors()
        extras = [e for e in errors if e.get("type") == "extra_forbidden"]
        non_extras = [e for e in errors if e.get("type") != "extra_forbidden"]
        if not extras or non_extras:
            raise RuntimeError(
                f"extractor returned invalid payload for {chapter_id}: {exc}"
            ) from exc

        cleaned = copy.deepcopy(payload) if isinstance(payload, dict) else payload
        stripped_paths: list[str] = []
        for err in extras:
            loc = err.get("loc", ())
            if _delete_at_path(cleaned, loc):
                stripped_paths.append(".".join(str(p) for p in loc))

        if stripped_paths:
            logger.warning(
                "mapper: stripped %d stray field(s) from %s payload: %s",
                len(stripped_paths),
                chapter_id,
                ", ".join(stripped_paths),
            )

        try:
            return ChapterExtraction.model_validate(cleaned)
        except ValidationError as exc2:
            raise RuntimeError(
                f"extractor returned invalid payload for {chapter_id} "
                f"(even after stripping extras): {exc2}"
            ) from exc2


def _delete_at_path(payload: Any, loc: tuple[Any, ...] | list[Any]) -> bool:
    """Delete the key identified by a Pydantic error ``loc`` path.

    Pydantic reports ``loc`` as a tuple of dict keys and list indices,
    e.g. ``("principles", 2, "source_quote")``. We walk to the parent of
    the final segment and delete the key there. Returns True when a key
    was actually removed, False if the path didn't resolve (e.g. because
    a prior fix already dropped a parent).
    """
    if not loc:
        return False
    parent: Any = payload
    for part in loc[:-1]:
        try:
            parent = parent[part]
        except (KeyError, IndexError, TypeError):
            return False
    last = loc[-1]
    if isinstance(parent, dict) and last in parent:
        del parent[last]
        return True
    return False


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
