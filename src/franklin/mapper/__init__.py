"""Stage 2: structured per-chapter extraction via the LLM (the 'map' stage).

Named `mapper` rather than `map` to avoid shadowing the Python builtin
inside the package.
"""

from franklin.mapper.extractor import (
    DEFAULT_MODEL,
    build_tool_schema,
    build_user_prompt,
    extract_chapter,
    extract_chapter_async,
    format_code_blocks,
)

__all__ = [
    "DEFAULT_MODEL",
    "build_tool_schema",
    "build_user_prompt",
    "extract_chapter",
    "extract_chapter_async",
    "format_code_blocks",
]
