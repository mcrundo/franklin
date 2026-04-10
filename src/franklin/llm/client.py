"""Anthropic client wrapper with forced tool-use.

The entire pipeline routes LLM calls through `call_tool`, which forces the
model to respond by calling a specific tool. This gives us structured output
that Pydantic can validate directly, eliminates the "model returned prose
around JSON" failure mode, and lets tests inject a fake client with no
special handling.

The `client` parameter is typed as Any so tests can pass a duck-typed stub
without implementing the full Anthropic SDK interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

DEFAULT_MAX_TOKENS = 16_000


@dataclass(frozen=True)
class ToolResult:
    """Parsed tool-use output from a forced Anthropic call."""

    input: dict[str, Any]
    stop_reason: str
    input_tokens: int
    output_tokens: int


def make_client() -> Anthropic:
    """Build an Anthropic client from environment configuration."""
    return Anthropic()


def call_tool(
    *,
    client: Any,
    model: str,
    system: str,
    user: str,
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ToolResult:
    """Call Claude with forced tool use and return the parsed tool input.

    Raises RuntimeError if the response does not contain a tool_use block,
    which should not happen when tool_choice is set but is worth surfacing
    clearly if it does.
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[
            {
                "name": tool_name,
                "description": tool_description,
                "input_schema": tool_schema,
            }
        ],
        tool_choice={"type": "tool", "name": tool_name},
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return ToolResult(
                input=dict(block.input),
                stop_reason=getattr(response, "stop_reason", "") or "",
                input_tokens=getattr(response.usage, "input_tokens", 0),
                output_tokens=getattr(response.usage, "output_tokens", 0),
            )

    raise RuntimeError(
        f"Expected tool_use block calling {tool_name!r}, got none in response"
    )
