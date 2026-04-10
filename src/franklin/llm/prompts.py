"""Prompt template loading.

Prompts live as markdown files under llm/prompts/ so they can be edited,
diffed, and reviewed like content. Placeholders use `{{name}}` syntax
substituted via plain string replacement — we deliberately avoid Python's
str.format and str.Template because technical book content is full of
literal `{` and `$` characters that would collide with those schemes.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without the .md extension)."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, /, **values: str) -> str:
    """Load a prompt template and substitute `{{name}}` placeholders."""
    text = load_prompt(name)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text
