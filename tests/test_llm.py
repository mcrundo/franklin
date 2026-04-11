"""Unit tests for LLM scaffolding — client wrapper and prompt loader."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from franklin.llm.client import cached_text_block, call_tool, text_block
from franklin.llm.prompts import load_prompt, render_prompt


class _FakeStream:
    """Context-manager stand-in for anthropic's streaming helper."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._response


class _FakeClient:
    """Minimal stand-in for anthropic.Anthropic for unit tests."""

    def __init__(self, tool_input: dict[str, Any]) -> None:
        self._tool_input = tool_input
        self.last_kwargs: dict[str, Any] | None = None
        self.messages = self

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.last_kwargs = kwargs
        return _FakeStream(
            SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input=self._tool_input)],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=100, output_tokens=50),
            )
        )


def test_call_tool_forces_tool_use_and_returns_parsed_input() -> None:
    expected = {"summary": "hello world"}
    client = _FakeClient(expected)

    result = call_tool(
        client=client,
        model="claude-sonnet-4-6",
        system="system prompt",
        user="user prompt",
        tool_name="save_thing",
        tool_description="Save a thing",
        tool_schema={"type": "object"},
    )

    assert result.input == expected
    assert result.input_tokens == 100
    assert result.output_tokens == 50

    assert client.last_kwargs is not None
    assert client.last_kwargs["tool_choice"] == {"type": "tool", "name": "save_thing"}
    assert client.last_kwargs["tools"][0]["name"] == "save_thing"


def test_call_tool_raises_when_no_tool_use_block() -> None:
    class NoToolClient:
        messages: Any

        def __init__(self) -> None:
            self.messages = self

        def stream(self, **_: Any) -> _FakeStream:
            return _FakeStream(
                SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="just prose")],
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=0, output_tokens=0),
                )
            )

    with pytest.raises(RuntimeError, match="tool_use"):
        call_tool(
            client=NoToolClient(),
            model="m",
            system="s",
            user="u",
            tool_name="save_thing",
            tool_description="d",
            tool_schema={"type": "object"},
        )


def test_render_prompt_substitutes_double_brace_placeholders(tmp_path: Any) -> None:
    import franklin.llm.prompts as prompts_module

    original = prompts_module.PROMPTS_DIR
    try:
        prompts_module.PROMPTS_DIR = tmp_path
        (tmp_path / "demo.md").write_text("hello {{name}}, value = {ruby: 1}", encoding="utf-8")
        rendered = render_prompt("demo", name="world")
        assert rendered == "hello world, value = {ruby: 1}"
    finally:
        prompts_module.PROMPTS_DIR = original


def test_load_prompt_missing_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("definitely_not_a_real_prompt_xyz")


def test_text_block_is_plain_content_block() -> None:
    block = text_block("hello")
    assert block == {"type": "text", "text": "hello"}


def test_cached_text_block_carries_ephemeral_cache_control() -> None:
    block = cached_text_block("stable prefix")
    assert block == {
        "type": "text",
        "text": "stable prefix",
        "cache_control": {"type": "ephemeral"},
    }


def test_call_tool_passes_structured_content_through() -> None:
    """When system and user are lists of content blocks, they're passed through
    to the SDK as-is so callers can mark any block with cache_control."""
    client = _FakeClient({"ok": True})
    call_tool(
        client=client,
        model="claude-sonnet-4-6",
        system=[cached_text_block("shared system prompt")],
        user=[
            cached_text_block("stable book context"),
            text_block("variable artifact brief"),
        ],
        tool_name="save",
        tool_description="save a thing",
        tool_schema={"type": "object"},
    )

    assert client.last_kwargs is not None
    system_arg = client.last_kwargs["system"]
    assert isinstance(system_arg, list)
    assert system_arg[0]["cache_control"] == {"type": "ephemeral"}

    user_content = client.last_kwargs["messages"][0]["content"]
    assert isinstance(user_content, list)
    assert user_content[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in user_content[1]


def test_tool_result_surfaces_cache_token_counts() -> None:
    """Cache read/creation tokens from the usage object should appear on the result."""

    class CachingStream:
        def __enter__(self) -> CachingStream:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def get_final_message(self) -> Any:
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input={})],
                stop_reason="tool_use",
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    cache_read_input_tokens=800,
                    cache_creation_input_tokens=200,
                ),
            )

    class CachingClient:
        messages: Any

        def __init__(self) -> None:
            self.messages = self

        def stream(self, **_: Any) -> CachingStream:
            return CachingStream()

    result = call_tool(
        client=CachingClient(),
        model="m",
        system="s",
        user="u",
        tool_name="t",
        tool_description="d",
        tool_schema={"type": "object"},
    )
    assert result.cache_read_tokens == 800
    assert result.cache_creation_tokens == 200
