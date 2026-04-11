"""YAML frontmatter validator for generated plugin files.

Every Claude Code plugin primitive that isn't a plain reference file has
a YAML frontmatter block with required fields:

- **SKILL.md** (`skills/<name>/SKILL.md`) — needs `name` and `description`
- **Command files** (`commands/*.md`) — need `description`
- **Agent files** (`agents/*.md`) — need `name` and `description`

This validator walks those file categories, parses the frontmatter block
(delimited by `---` markers at the very top of the file), and reports any
missing block, any YAML parse error, and any required field that is
missing or not a non-empty string. Pure Python, no LLM calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Required string fields per file category. Lists of required fields only —
# optional fields like allowed-tools, argument-hint, model are not enforced
# here because they're non-essential for the plugin to load.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "skill": ("name", "description"),
    "command": ("description",),
    "agent": ("name", "description"),
}


@dataclass(frozen=True)
class FrontmatterIssue:
    """One problem found in a file's YAML frontmatter."""

    source_file: Path
    category: str
    kind: str
    message: str


def validate_frontmatter(plugin_root: Path) -> list[FrontmatterIssue]:
    """Return every frontmatter issue found across SKILL, command, and agent files."""
    issues: list[FrontmatterIssue] = []

    for skill_md in sorted(plugin_root.glob("skills/*/SKILL.md")):
        issues.extend(_validate_file(skill_md, "skill"))

    commands_dir = plugin_root / "commands"
    if commands_dir.is_dir():
        for cmd_md in sorted(commands_dir.glob("*.md")):
            issues.extend(_validate_file(cmd_md, "command"))

    agents_dir = plugin_root / "agents"
    if agents_dir.is_dir():
        for agent_md in sorted(agents_dir.glob("*.md")):
            issues.extend(_validate_file(agent_md, "agent"))

    return issues


def _validate_file(path: Path, category: str) -> list[FrontmatterIssue]:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)

    if not match:
        return [
            FrontmatterIssue(
                source_file=path,
                category=category,
                kind="missing",
                message="no YAML frontmatter block at top of file",
            )
        ]

    try:
        data: Any = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [
            FrontmatterIssue(
                source_file=path,
                category=category,
                kind="unparseable",
                message=f"YAML parse error: {exc}",
            )
        ]

    if not isinstance(data, dict):
        return [
            FrontmatterIssue(
                source_file=path,
                category=category,
                kind="unparseable",
                message=f"frontmatter is not a mapping (got {type(data).__name__})",
            )
        ]

    issues: list[FrontmatterIssue] = []
    for field in _REQUIRED_FIELDS[category]:
        if field not in data:
            issues.append(
                FrontmatterIssue(
                    source_file=path,
                    category=category,
                    kind="field-missing",
                    message=f"missing required field {field!r}",
                )
            )
            continue
        value = data[field]
        if not isinstance(value, str) or not value.strip():
            issues.append(
                FrontmatterIssue(
                    source_file=path,
                    category=category,
                    kind="field-wrong-type",
                    message=f"field {field!r} must be a non-empty string",
                )
            )

    return issues
