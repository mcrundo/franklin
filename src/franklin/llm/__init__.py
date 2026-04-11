"""LLM scaffolding: Anthropic client wrapper and prompt loading."""

from franklin.llm.client import (
    ToolResult,
    cached_text_block,
    call_tool,
    make_client,
    text_block,
)
from franklin.llm.prompts import load_prompt, render_prompt

__all__ = [
    "ToolResult",
    "cached_text_block",
    "call_tool",
    "load_prompt",
    "make_client",
    "render_prompt",
    "text_block",
]
