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

# max_tokens is a safety ceiling, not a billing amount — you only pay for
# what the model actually generates. We set this high enough that even the
# longest chapters can't be truncated mid-extraction. A dedicated cost
# estimator (scaling from word count and code block count) will land later
# as its own feature to show predicted spend before a run kicks off.
DEFAULT_MAX_TOKENS = 32_000


@dataclass(frozen=True)
class ToolResult:
    """Parsed tool-use output from a forced Anthropic call."""

    input: dict[str, Any]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


def make_client() -> Anthropic:
    """Build an Anthropic client from environment configuration."""
    return Anthropic()


def text_block(value: str) -> dict[str, Any]:
    """Build a plain text content block."""
    return {"type": "text", "text": value}


def cached_text_block(value: str) -> dict[str, Any]:
    """Build a text content block marked for ephemeral prompt caching.

    Anthropic's prompt caching stores the content at this breakpoint for
    ~5 minutes; subsequent calls within that window that repeat the same
    prefix pay roughly 10% of the normal input cost for the cached tokens.
    Use this for content that is stable across many calls in a single run
    — system prompts, book metadata, coherence rules, the bulky distilled
    sidecar slice — and leave the per-call variable tail uncached.
    """
    return {
        "type": "text",
        "text": value,
        "cache_control": {"type": "ephemeral"},
    }


def call_tool(
    *,
    client: Any,
    model: str,
    system: str | list[dict[str, Any]],
    user: str | list[dict[str, Any]],
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ToolResult:
    """Call Claude with forced tool use and return the parsed tool input.

    The `system` and `user` parameters accept either a plain string (no
    caching, simple case) or a list of content blocks. Pass content blocks
    built with cached_text_block() to enable prompt caching for the stable
    prefix of a repeated call pattern — typically how the reduce stage
    amortizes its system prompt and book context across many generations.

    Uses the streaming entrypoint (`messages.stream`) because the SDK
    refuses non-streaming calls whose max_tokens could in principle exceed
    a 10-minute wall-clock budget. Streaming collects the same final
    message — we only read it after the stream completes, so the behavior
    is identical to a .create() call from the caller's perspective.

    Raises RuntimeError if the response does not contain a tool_use block,
    which should not happen when tool_choice is set but is worth surfacing
    clearly if it does.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [
            {
                "name": tool_name,
                "description": tool_description,
                "input_schema": tool_schema,
            }
        ],
        "tool_choice": {"type": "tool", "name": tool_name},
    }

    with client.messages.stream(**kwargs) as stream:
        response = stream.get_final_message()

    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            usage = getattr(response, "usage", None)
            return ToolResult(
                input=dict(block.input),
                stop_reason=getattr(response, "stop_reason", "") or "",
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
                cache_creation_tokens=(
                    getattr(usage, "cache_creation_input_tokens", 0) if usage else 0
                ),
            )

    raise RuntimeError(f"Expected tool_use block calling {tool_name!r}, got none in response")
