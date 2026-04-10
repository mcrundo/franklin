"""Unit tests for LLM scaffolding — client wrapper and prompt loader."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from franklin.llm.client import call_tool
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
