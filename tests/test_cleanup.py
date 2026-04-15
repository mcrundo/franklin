"""Tests for franklin.ingest.cleanup (Tier 4 LLM cleanup pass)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from _fakes import FakeClient
from franklin.ingest.cleanup import clean_chapter, clean_chapters
from franklin.schema import CodeBlock, NormalizedChapter


def _client(cleaned_text: str, *, input_tokens: int = 100, output_tokens: int = 80) -> FakeClient:
    return FakeClient(
        {"cleaned_text": cleaned_text},
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
    )


class _RaisingClient:
    """Simulates a network failure — can't use FakeClient since this
    needs to raise before ever yielding a stream."""

    def __init__(self) -> None:
        self.messages = self

    def stream(self, **_: Any) -> Any:
        raise RuntimeError("anthropic: simulated network failure")


def _chapter(
    chapter_id: str = "ch01",
    *,
    text: str = "ButthereisonepartofeveryappthatRailsdoesnthaveaclear answer.",
    code_blocks: list[CodeBlock] | None = None,
) -> NormalizedChapter:
    return NormalizedChapter(
        chapter_id=chapter_id,
        title=f"Chapter {chapter_id}",
        order=int(chapter_id.removeprefix("ch")),
        source_ref=f"pp. {chapter_id}",
        word_count=len(text.split()),
        text=text,
        code_blocks=code_blocks or [],
        headings=[f"Chapter {chapter_id}"],
    )


# ---------------------------------------------------------------------------
# clean_chapter happy path
# ---------------------------------------------------------------------------


def test_clean_chapter_replaces_text_with_cleaned_version() -> None:
    chapter = _chapter()
    client = _client("But there is one part of every app that Rails doesn't have a clear answer.")
    result, in_toks, out_toks = clean_chapter(chapter, client=client)

    assert "ButthereisonepartofeveryappthatRails" not in result.text
    assert "But there is one part" in result.text
    assert in_toks == 100
    assert out_toks == 80


def test_clean_chapter_recomputes_word_count() -> None:
    chapter = _chapter(text="ButthereisonepartofeveryappthatRails")
    assert chapter.word_count == 1  # Tier 2 counted it as a single jumbled token

    client = _client("But there is one part of every app that Rails")
    result, _, _ = clean_chapter(chapter, client=client)
    assert result.word_count == 10


def test_clean_chapter_preserves_code_blocks_verbatim() -> None:
    code = CodeBlock(language="ruby", code="class PostPublishService\n  def call; end\nend")
    chapter = _chapter(code_blocks=[code])

    # The fake client returns only cleaned prose; code blocks can't be touched
    # by the tool because they aren't in the schema.
    client = _client("cleaned prose")
    result, _, _ = clean_chapter(chapter, client=client)

    assert result.code_blocks == chapter.code_blocks
    assert result.code_blocks[0].code == code.code


def test_clean_chapter_preserves_identity_fields() -> None:
    chapter = _chapter(chapter_id="ch07")
    client = _client("cleaned")
    result, _, _ = clean_chapter(chapter, client=client)

    assert result.chapter_id == "ch07"
    assert result.title == "Chapter ch07"
    assert result.order == 7
    assert result.source_ref == chapter.source_ref
    assert result.headings == chapter.headings


def test_clean_chapter_forces_tool_choice() -> None:
    chapter = _chapter()
    client = _client("cleaned prose")
    clean_chapter(chapter, client=client)

    assert client.last_kwargs is not None
    assert client.last_kwargs["tool_choice"] == {
        "type": "tool",
        "name": "save_cleaned_chapter",
    }
    tools = client.last_kwargs["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "save_cleaned_chapter"
    assert "cleaned_text" in tools[0]["input_schema"]["properties"]


def test_clean_chapter_rejects_empty_cleaned_text() -> None:
    chapter = _chapter()
    client = _client("   ")
    with pytest.raises(RuntimeError, match="empty text"):
        clean_chapter(chapter, client=client)


def test_clean_chapter_propagates_tool_failure() -> None:
    chapter = _chapter()
    with pytest.raises(RuntimeError, match="simulated network"):
        clean_chapter(chapter, client=_RaisingClient())


# ---------------------------------------------------------------------------
# clean_chapters batch helper
# ---------------------------------------------------------------------------


def test_clean_chapters_processes_every_chapter() -> None:
    chapters = [
        _chapter("ch01", text="some text"),
        _chapter("ch02", text="more text"),
        _chapter("ch03", text="final text"),
    ]
    client = _client("cleaned")

    cleaned, in_toks, out_toks, failed = clean_chapters(chapters, client=client)
    assert len(cleaned) == 3
    assert all(c.text == "cleaned" for c in cleaned)
    assert in_toks == 300  # 100 per chapter
    assert out_toks == 240  # 80 per chapter
    assert failed == []


def test_clean_chapters_non_fatal_failure_keeps_original() -> None:
    chapters = [_chapter("ch01"), _chapter("ch02"), _chapter("ch03")]

    call_count = {"n": 0}
    originals = {c.chapter_id: c for c in chapters}

    def fake_clean(
        chapter: NormalizedChapter,
        *,
        client: Any | None = None,
        model: str = "claude-sonnet-4-6",
        max_tokens: int | None = None,
    ) -> tuple[NormalizedChapter, int, int]:
        call_count["n"] += 1
        if chapter.chapter_id == "ch02":
            raise RuntimeError("simulated failure for ch02")
        return (
            NormalizedChapter(
                chapter_id=chapter.chapter_id,
                title=chapter.title,
                order=chapter.order,
                source_ref=chapter.source_ref,
                word_count=10,
                text="cleaned",
                code_blocks=chapter.code_blocks,
                headings=chapter.headings,
            ),
            50,
            40,
        )

    with patch("franklin.ingest.cleanup.clean_chapter", side_effect=fake_clean):
        cleaned, in_toks, out_toks, failed = clean_chapters(chapters)

    assert [c.chapter_id for c in cleaned] == ["ch01", "ch02", "ch03"]
    assert cleaned[0].text == "cleaned"
    assert cleaned[1] is originals["ch02"]  # preserved original on failure
    assert cleaned[2].text == "cleaned"
    assert failed == ["ch02"]
    assert in_toks == 100  # only two successful chapters contributed
    assert out_toks == 80
    assert call_count["n"] == 3


def test_clean_chapters_reports_progress_and_failure() -> None:
    chapters = [_chapter("ch01"), _chapter("ch02")]
    progress_calls: list[str] = []
    failure_calls: list[tuple[str, str]] = []

    def fake_clean(
        chapter: NormalizedChapter,
        *,
        client: Any | None = None,
        model: str = "claude-sonnet-4-6",
        max_tokens: int | None = None,
    ) -> tuple[NormalizedChapter, int, int]:
        if chapter.chapter_id == "ch02":
            raise RuntimeError("boom")
        return chapter, 10, 5

    with patch("franklin.ingest.cleanup.clean_chapter", side_effect=fake_clean):
        clean_chapters(
            chapters,
            on_progress=lambda c: progress_calls.append(c.chapter_id),
            on_failure=lambda c, exc: failure_calls.append((c.chapter_id, str(exc))),
        )

    assert progress_calls == ["ch01", "ch02"]
    assert len(failure_calls) == 1
    assert failure_calls[0][0] == "ch02"
    assert "boom" in failure_calls[0][1]
