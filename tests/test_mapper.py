"""Tests for the map-stage chapter extractor.

Covers prompt building, tool-schema generation, and a full extract_chapter
round-trip against a fake Anthropic client. A separate live test runs the
real API against Layered Design Chapter 3 and is skipped unless
ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from franklin.ingest import ingest_epub
from franklin.mapper import (
    build_tool_schema,
    build_user_prompt,
    extract_chapter,
    format_code_blocks,
)
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    CodeBlock,
    NormalizedChapter,
)

FIXTURE = Path(__file__).resolve().parents[1] / (
    "Layered Design for Ruby on Rails Applications by Vladimir Dementyev.epub"
)


def _book() -> BookManifest:
    return BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path="x.epub", sha256="0" * 64, format="epub", ingested_at=datetime.now(UTC)
        ),
        metadata=BookMetadata(title="Test Book", authors=["Ada Lovelace"]),
        structure=BookStructure(),
    )


def _chapter() -> NormalizedChapter:
    return NormalizedChapter(
        chapter_id="ch03",
        title="Service Objects",
        order=3,
        source_ref="OEBPS/ch03.xhtml",
        word_count=42,
        text="Body text mentioning { braces } and $dollars stays verbatim.",
        code_blocks=[
            CodeBlock(language="ruby", code="class X\n  def call; end\nend"),
            CodeBlock(language=None, code="plain text block"),
        ],
    )


def test_format_code_blocks_labels_and_fences() -> None:
    out = format_code_blocks(_chapter().code_blocks)
    assert "### code-block-1 (ruby)" in out
    assert "### code-block-2 (text)" in out
    assert "```ruby" in out


def test_format_code_blocks_empty() -> None:
    assert "no code blocks" in format_code_blocks([])


def test_build_user_prompt_substitutes_placeholders_literally() -> None:
    prompt = build_user_prompt(_book(), _chapter())
    assert "Test Book" in prompt
    assert "Ada Lovelace" in prompt
    assert "Service Objects" in prompt
    assert "ch03" in prompt
    # Chapter content containing literal curly braces and dollar signs
    # must survive the templating untouched.
    assert "{ braces }" in prompt
    assert "$dollars" in prompt
    # No unresolved placeholders should remain.
    assert "{{" not in prompt


def test_build_tool_schema_is_an_object_schema() -> None:
    schema = build_tool_schema()
    assert schema["type"] == "object"
    assert "summary" in schema["properties"]
    for category in (
        "concepts",
        "principles",
        "rules",
        "anti_patterns",
        "code_examples",
        "decision_rules",
        "actionable_workflows",
        "terminology",
        "cross_references",
    ):
        assert category in schema["properties"], f"missing category {category}"


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.messages = self
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input=self._payload)],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=123, output_tokens=456),
        )


def test_extract_chapter_merges_with_ingest_metadata() -> None:
    book = _book()
    chapter = _chapter()
    payload = {
        "summary": "Explains service objects.",
        "concepts": [
            {
                "id": "ch03.concept.service-object",
                "name": "Service Object",
                "definition": "A plain Ruby object encapsulating one operation",
                "importance": "high",
                "source_location": "ch03 opening",
            }
        ],
    }
    client = _FakeClient(payload)

    sidecar, in_toks, out_toks = extract_chapter(book, chapter, client=client)

    assert sidecar.chapter_id == "ch03"
    assert sidecar.title == "Service Objects"
    assert sidecar.order == 3
    assert sidecar.word_count == 42
    assert sidecar.summary == "Explains service objects."
    assert len(sidecar.concepts) == 1
    assert sidecar.concepts[0].name == "Service Object"
    assert in_toks == 123
    assert out_toks == 456

    assert client.last_kwargs is not None
    assert client.last_kwargs["tool_choice"]["name"] == "save_chapter_extraction"


def test_extract_chapter_rejects_invalid_payload() -> None:
    bad_payload = {"concepts": [{"id": "missing fields"}]}  # no summary, invalid concept
    client = _FakeClient(bad_payload)
    with pytest.raises(RuntimeError, match="invalid payload"):
        extract_chapter(_book(), _chapter(), client=client)


# ---------------------------------------------------------------------------
# Live API test — runs only when ANTHROPIC_API_KEY is set.
# Gated further by FRANKLIN_LIVE_API=1 to avoid surprise charges.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("FRANKLIN_LIVE_API"),
    reason="live API test requires ANTHROPIC_API_KEY and FRANKLIN_LIVE_API=1",
)
def test_extract_layered_design_chapter_3_live() -> None:
    if not FIXTURE.exists():
        pytest.skip(f"fixture not found: {FIXTURE}")

    manifest, chapters = ingest_epub(FIXTURE)
    chapter = next(c for c in chapters if c.title.startswith("Chapter 3:"))

    sidecar, in_toks, out_toks = extract_chapter(manifest, chapter)

    assert sidecar.summary
    assert sidecar.concepts or sidecar.principles or sidecar.rules
    assert in_toks > 0
    assert out_toks > 0
    print(f"\n[live] {chapter.title}: {in_toks} in / {out_toks} out tokens")
    print(f"[live] summary: {sidecar.summary}")
