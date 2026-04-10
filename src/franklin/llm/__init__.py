"""LLM scaffolding: Anthropic client wrapper and prompt loading."""

from franklin.llm.client import ToolResult, call_tool, make_client
from franklin.llm.prompts import load_prompt, render_prompt

__all__ = ["ToolResult", "call_tool", "load_prompt", "make_client", "render_prompt"]
