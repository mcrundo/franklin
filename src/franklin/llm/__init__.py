"""LLM scaffolding: Anthropic client wrapper and prompt loading."""

from franklin.llm.client import (
    ToolResult,
    cached_text_block,
    call_tool,
    call_tool_async,
    make_async_client,
    make_client,
    text_block,
)
from franklin.llm.prompts import load_prompt, render_prompt
from franklin.llm.validation import validate_with_extra_recovery

__all__ = [
    "ToolResult",
    "cached_text_block",
    "call_tool",
    "call_tool_async",
    "load_prompt",
    "make_async_client",
    "make_client",
    "render_prompt",
    "text_block",
    "validate_with_extra_recovery",
]
