"""Tests for the map-stage chapter extractor.

Covers prompt building, tool-schema generation, and a full extract_chapter
round-trip against a fake Anthropic client. A separate live test runs the
real API against Layered Design Chapter 3 and is skipped unless
ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import asyncio
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
    extract_chapter_async,
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


class _FakeStream:
    def __init__(self, response: Any) -> None:
        self._response = response

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._response


class _FakeAsyncStream:
    """Async counterpart of _FakeStream for testing async extract/generate."""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeAsyncStream:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get_final_message(self) -> Any:
        return self._response


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.messages = self
        self.last_kwargs: dict[str, Any] | None = None

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.last_kwargs = kwargs
        return _FakeStream(
            SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input=self._payload)],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=123, output_tokens=456),
            )
        )


class _FakeAsyncClient:
    """Async counterpart of _FakeClient for testing async extract/generate."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.messages = self

    def stream(self, **kwargs: Any) -> _FakeAsyncStream:
        return _FakeAsyncStream(
            SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input=self._payload)],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=100, output_tokens=200),
            )
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


def test_extract_chapter_async_round_trip() -> None:
    """Async extract produces the same sidecar shape as the sync version."""
    book = _book()
    chapter = _chapter()
    payload = {
        "summary": "Async test.",
        "concepts": [
            {
                "id": "ch03.concept.async",
                "name": "Async",
                "definition": "Concurrent extraction",
                "importance": "high",
                "source_location": "ch03 opening",
            }
        ],
    }
    client = _FakeAsyncClient(payload)
    sidecar, in_toks, out_toks = asyncio.run(extract_chapter_async(book, chapter, client=client))
    assert sidecar.chapter_id == "ch03"
    assert sidecar.summary == "Async test."
    assert len(sidecar.concepts) == 1
    assert in_toks == 100
    assert out_toks == 200


def test_extract_chapter_rejects_invalid_payload() -> None:
    bad_payload = {"concepts": [{"id": "missing fields"}]}  # no summary, invalid concept
    client = _FakeClient(bad_payload)
    with pytest.raises(RuntimeError, match="invalid payload"):
        extract_chapter(_book(), _chapter(), client=client)


def test_extract_chapter_recovers_from_stray_extra_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LLMs sometimes slip an extra field onto a sub-object (e.g. source_quote
    on a Principle, generalized from Concept). We keep the schema strict
    going out, but strip stray extras coming back so one drifted field
    doesn't kill the whole chapter."""
    payload = {
        "summary": "A chapter about pragmatism.",
        "principles": [
            {
                "id": "ch03.principle.dry",
                "statement": "Don't Repeat Yourself",
                "rationale": "Duplication makes change expensive",
                "source_location": "ch03 §1",
                "source_quote": "Every piece of knowledge must have a single representation",
            },
        ],
    }
    client = _FakeClient(payload)

    with caplog.at_level("WARNING", logger="franklin.mapper.extractor"):
        sidecar, _, _ = extract_chapter(_book(), _chapter(), client=client)

    assert sidecar.summary == "A chapter about pragmatism."
    assert len(sidecar.principles) == 1
    assert sidecar.principles[0].statement == "Don't Repeat Yourself"
    # The stripped field is logged so drift is visible in run logs.
    assert any("source_quote" in r.message for r in caplog.records)


def test_extract_chapter_recovers_from_multiple_stray_extras() -> None:
    """Stripping handles multiple extras across different list items."""
    payload = {
        "summary": "Multi-extra payload.",
        "principles": [
            {
                "id": "p1",
                "statement": "First",
                "source_location": "§1",
                "bogus_field": "x",
            },
            {
                "id": "p2",
                "statement": "Second",
                "source_location": "§2",
                "another_bogus": "y",
            },
        ],
    }
    client = _FakeClient(payload)
    sidecar, _, _ = extract_chapter(_book(), _chapter(), client=client)
    assert [p.id for p in sidecar.principles] == ["p1", "p2"]


def test_extract_chapter_recovers_from_stringified_json_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LLMs occasionally return a JSON string where a list is expected
    (e.g. anti_patterns as '"[{...}]"' instead of [{...}]). The
    validator should deserialize the string and proceed."""
    import json

    anti_patterns_list = [
        {
            "id": "ch03.anti.god-service",
            "name": "God Service",
            "description": "A service that does too much",
            "fix": "Split by domain",
            "source_location": "ch03 §4",
        },
    ]
    payload = {
        "summary": "A chapter with stringified anti_patterns.",
        "anti_patterns": json.dumps(anti_patterns_list),
    }
    client = _FakeClient(payload)

    with caplog.at_level("WARNING", logger="franklin.llm.validation"):
        sidecar, _, _ = extract_chapter(_book(), _chapter(), client=client)

    assert len(sidecar.anti_patterns) == 1
    assert sidecar.anti_patterns[0].name == "God Service"
    assert any("stringified JSON" in r.message for r in caplog.records)


class _SequenceClient:
    """Fake client that returns a different payload on each call."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = list(payloads)
        self.messages = self
        self.call_count = 0

    def stream(self, **kwargs: Any) -> _FakeStream:
        payload = self._payloads[self.call_count]
        self.call_count += 1
        return _FakeStream(
            SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input=payload)],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=10, output_tokens=20),
            )
        )


def test_extract_chapter_retries_on_validation_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient invalid payload should be retried, not fatally raised."""
    bad = {"concepts": [{"id": "missing fields"}]}
    good = {"summary": "Recovered on retry."}
    client = _SequenceClient([bad, good])

    with caplog.at_level("WARNING", logger="franklin.mapper.extractor"):
        sidecar, in_toks, out_toks = extract_chapter(_book(), _chapter(), client=client)

    assert client.call_count == 2
    assert sidecar.summary == "Recovered on retry."
    # Token counts accumulate across retries so callers see the true cost.
    assert in_toks == 20
    assert out_toks == 40
    assert any("retrying" in r.message for r in caplog.records)


def test_extract_chapter_gives_up_after_max_attempts() -> None:
    bad = {"concepts": [{"id": "missing fields"}]}
    client = _SequenceClient([bad, bad, bad])
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        extract_chapter(_book(), _chapter(), client=client)
    assert client.call_count == 3


def test_extract_chapter_recovers_from_stringified_json_with_literal_newlines(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stringified JSON list with unescaped newlines inside string values
    is invalid per spec but common from LLMs — the lenient parser should
    still recover it instead of forcing a retry."""
    payload = {
        "summary": "Stringified payload with literal newlines.",
        "anti_patterns": (
            '[\n  {\n    "id": "ch03.anti.x",\n    "name": "Newline",\n    '
            '"description": "Has a\nliteral newline inside",\n    '
            '"fix": "Escape it",\n    "source_location": "ch03 §1"\n  }\n]'
        ),
    }
    client = _FakeClient(payload)

    with caplog.at_level("WARNING", logger="franklin.llm.validation"):
        sidecar, _, _ = extract_chapter(_book(), _chapter(), client=client)

    assert len(sidecar.anti_patterns) == 1
    assert sidecar.anti_patterns[0].name == "Newline"


def test_extract_chapter_still_rejects_non_extra_errors() -> None:
    """Missing required fields are NOT recoverable — only stray extras are."""
    payload = {
        "summary": "Missing required subfield.",
        "principles": [
            {
                "id": "p1",
                # missing required `statement` and `source_location`
                "rogue": "still bad",
            }
        ],
    }
    client = _FakeClient(payload)
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
