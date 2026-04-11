"""Stage 3: design a Claude Code plugin from distilled sidecars (the 'plan' stage)."""

from franklin.planner.designer import (
    DEFAULT_MODEL,
    build_distilled_view,
    build_tool_schema,
    build_user_prompt,
    design_plan,
)

__all__ = [
    "DEFAULT_MODEL",
    "build_distilled_view",
    "build_tool_schema",
    "build_user_prompt",
    "design_plan",
]
