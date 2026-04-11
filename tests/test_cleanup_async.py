"""Tests for the async cleanup path (RUB-92).

These exercise ``clean_chapter_async`` and ``clean_chapters_async`` against
a fake async client. The sync tests in ``test_cleanup.py`` stay unchanged
— the async functions share parsing and prompt-building helpers with the
sync path, so correctness there covers both.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from franklin.ingest.cleanup import (
    DEFAULT_CLEANUP_CONCURRENCY,
    clean_chapter_async,
    clean_chapters_async,
)
from franklin.schema import CodeBlock, NormalizedChapter

# ---------------------------------------------------------------------------
# Fake async client
# ---------------------------------------------------------------------------


class _FakeAsyncStream:
    """Mirror of AsyncMessageStream for unit tests — async context + await."""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeAsyncStream:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get_final_message(self) -> Any:
        return self._response


class _FakeAsyncMessages:
    def __init__(
        self,
        client: _FakeAsyncClient,
    ) -> None:
        self._client = client

    def stream(self, **kwargs: Any) -> _FakeAsyncStream:
        return self._client._stream(**kwargs)


class _FakeAsyncClient:
    """Fake ``AsyncAnthropic`` that records calls and optionally delays."""

    def __init__(
        self,
        cleaned_text: str | None = "cleaned prose",
        *,
        per_call_delay: float = 0.0,
        input_tokens: int = 100,
        output_tokens: int = 80,
        fail_on: set[str] | None = None,
    ) -> None:
        self._cleaned_text = cleaned_text
        self._per_call_delay = per_call_delay
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._fail_on = fail_on or set()
        self.messages = _FakeAsyncMessages(self)
        self.call_log: list[dict[str, Any]] = []
        self._in_flight = 0
        self._max_in_flight = 0
        self._lock = asyncio.Lock()

    def _stream(self, **kwargs: Any) -> _FakeAsyncStream:
        self.call_log.append(kwargs)
        body_text = self._extract_chapter_text(kwargs)
        return _FakeAsyncStream(_ResponseFactory(self, body_text))

    def _extract_chapter_text(self, kwargs: dict[str, Any]) -> str:
        """Pull the chapter text out of the rendered prompt so fail_on works."""
        user = kwargs.get("messages", [{}])[0].get("content", "")
        return user if isinstance(user, str) else ""


class _ResponseFactory:
    """Returned by _FakeAsyncStream.get_final_message via its own await chain."""

    def __init__(self, client: _FakeAsyncClient, body: str) -> None:
        self._client = client
        self._body = body

    def __await__(self) -> Any:
        return self._get_final_message().__await__()

    async def _get_final_message(self) -> Any:
        async with self._client._lock:
            self._client._in_flight += 1
            self._client._max_in_flight = max(self._client._max_in_flight, self._client._in_flight)

        try:
            if self._client._per_call_delay:
                await asyncio.sleep(self._client._per_call_delay)

            if any(marker in self._body for marker in self._client._fail_on):
                raise RuntimeError("simulated network failure")

            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        input={"cleaned_text": self._client._cleaned_text or ""},
                    )
                ],
                stop_reason="tool_use",
                usage=SimpleNamespace(
                    input_tokens=self._client._input_tokens,
                    output_tokens=self._client._output_tokens,
                ),
            )
        finally:
            async with self._client._lock:
                self._client._in_flight -= 1


# Override `_FakeAsyncStream.get_final_message` so `await stream.get_final_message()`
# returns the factory, which itself is awaitable.
async def _fake_get_final_message(self: _FakeAsyncStream) -> Any:  # type: ignore[override]
    return await self._response  # type: ignore[misc]


_FakeAsyncStream.get_final_message = _fake_get_final_message  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _chapter(
    chapter_id: str = "ch01",
    *,
    text: str = "ButthereisonepartofeveryappthatRails.",
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
# clean_chapter_async
# ---------------------------------------------------------------------------


def test_clean_chapter_async_happy_path() -> None:
    async def run() -> None:
        client = _FakeAsyncClient("But there is one part of every app that Rails.")
        chapter = _chapter()
        result, in_toks, out_toks = await clean_chapter_async(chapter, client=client)
        assert "But there is one part" in result.text
        assert in_toks == 100
        assert out_toks == 80
        assert len(client.call_log) == 1
        assert client.call_log[0]["tool_choice"] == {
            "type": "tool",
            "name": "save_cleaned_chapter",
        }

    asyncio.run(run())


def test_clean_chapter_async_preserves_code_blocks() -> None:
    async def run() -> None:
        client = _FakeAsyncClient("cleaned")
        code = CodeBlock(language="ruby", code="class X; end")
        chapter = _chapter(code_blocks=[code])
        result, _, _ = await clean_chapter_async(chapter, client=client)
        assert result.code_blocks == chapter.code_blocks

    asyncio.run(run())


def test_clean_chapter_async_rejects_empty_payload() -> None:
    async def run() -> None:
        client = _FakeAsyncClient("   ")
        with pytest.raises(RuntimeError, match="empty text"):
            await clean_chapter_async(_chapter(), client=client)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# clean_chapters_async — concurrency + failure handling
# ---------------------------------------------------------------------------


def test_clean_chapters_async_processes_every_chapter_in_order() -> None:
    async def run() -> None:
        chapters = [_chapter(f"ch{i:02d}", text=f"text_{i}") for i in range(1, 6)]
        client = _FakeAsyncClient("cleaned")
        cleaned, in_toks, out_toks, failed = await clean_chapters_async(
            chapters, client=client, concurrency=3
        )
        assert [c.chapter_id for c in cleaned] == [
            "ch01",
            "ch02",
            "ch03",
            "ch04",
            "ch05",
        ]
        assert in_toks == 500
        assert out_toks == 400
        assert failed == []

    asyncio.run(run())


def test_clean_chapters_async_respects_concurrency_limit() -> None:
    async def run() -> None:
        chapters = [_chapter(f"ch{i:02d}") for i in range(1, 11)]
        client = _FakeAsyncClient("cleaned", per_call_delay=0.05)
        await clean_chapters_async(chapters, client=client, concurrency=4)
        assert client._max_in_flight <= 4
        assert client._max_in_flight >= 2  # concurrency actually happened

    asyncio.run(run())


def test_clean_chapters_async_non_fatal_failure_keeps_original() -> None:
    async def run() -> None:
        chapters = [_chapter(f"ch{i:02d}", text=f"body_{i}") for i in range(1, 4)]
        # Fail only the chapter whose text contains "body_2"
        client = _FakeAsyncClient("cleaned", fail_on={"body_2"})
        cleaned, in_toks, out_toks, failed = await clean_chapters_async(
            chapters, client=client, concurrency=3
        )
        assert failed == ["ch02"]
        # ch02 kept its original text; ch01 and ch03 got the cleaned text
        assert cleaned[0].text == "cleaned"
        assert cleaned[1].text == "body_2"
        assert cleaned[2].text == "cleaned"
        assert in_toks == 200  # only two successful contributed
        assert out_toks == 160

    asyncio.run(run())


def test_clean_chapters_async_on_progress_fires_per_success() -> None:
    async def run() -> None:
        chapters = [_chapter("ch01"), _chapter("ch02"), _chapter("ch03")]
        client = _FakeAsyncClient("cleaned", fail_on={"ch02"})
        seen: list[str] = []

        def on_progress(chapter: NormalizedChapter) -> None:
            seen.append(chapter.chapter_id)

        failures: list[str] = []

        def on_failure(chapter: NormalizedChapter, _exc: Exception) -> None:
            failures.append(chapter.chapter_id)

        await clean_chapters_async(
            chapters,
            client=client,
            concurrency=3,
            on_progress=on_progress,
            on_failure=on_failure,
        )
        assert set(seen) == {"ch01", "ch03"}
        assert failures == ["ch02"]

    asyncio.run(run())


def test_clean_chapters_async_rejects_invalid_concurrency() -> None:
    async def run() -> None:
        with pytest.raises(ValueError, match="concurrency must be"):
            await clean_chapters_async([_chapter()], client=_FakeAsyncClient("x"), concurrency=0)

    asyncio.run(run())


def test_default_concurrency_constant_is_sensible() -> None:
    assert DEFAULT_CLEANUP_CONCURRENCY == 8
