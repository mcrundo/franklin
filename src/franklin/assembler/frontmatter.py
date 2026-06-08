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

    preamble: list[FrontmatterIssue] = []
    if _description_is_multiline(match.group(1)):
        preamble.append(
            FrontmatterIssue(
                source_file=path,
                category=category,
                kind="description-multiline",
                message=(
                    "description must be a single-line scalar — no YAML block "
                    "scalar ('>' or '|') or wrapped value (breaks naive loaders)"
                ),
            )
        )

    try:
        data: Any = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        repaired = _try_repair_yaml(match.group(1))
        if repaired is not None:
            data = repaired
        else:
            try:
                # Re-parse for the error message since we consumed the first exc.
                yaml.safe_load(match.group(1))
            except yaml.YAMLError as exc2:
                return [
                    *preamble,
                    FrontmatterIssue(
                        source_file=path,
                        category=category,
                        kind="unparseable",
                        message=f"YAML parse error: {exc2}",
                    ),
                ]

    if not isinstance(data, dict):
        return [
            *preamble,
            FrontmatterIssue(
                source_file=path,
                category=category,
                kind="unparseable",
                message=f"frontmatter is not a mapping (got {type(data).__name__})",
            ),
        ]

    issues: list[FrontmatterIssue] = list(preamble)
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


_SIMPLE_KV_RE = re.compile(r"^([a-z][a-z_-]*)\s*:\s*(.+)$", re.IGNORECASE)


def _try_repair_yaml(raw: str) -> dict[str, Any] | None:
    """Heuristic repair for LLM-generated YAML frontmatter.

    The most common failure mode: the LLM puts unquoted colons in a
    description value (e.g. ``null: false, foreign_key: true``), which
    YAML interprets as nested mapping keys. This repair quotes every
    scalar value that isn't already quoted, then re-parses.

    Returns the parsed dict on success, or None if repair didn't help.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        m = _SIMPLE_KV_RE.match(line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if not (value.startswith('"') or value.startswith("'") or value.startswith(">")):
                value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
            lines.append(f"{key}: {value}")
        else:
            lines.append(line)
    try:
        data = yaml.safe_load("\n".join(lines))
    except yaml.YAMLError:
        return None
    if isinstance(data, dict):
        return data
    return None


_DESCRIPTION_KEY_RE = re.compile(r"^(\s*)description:\s*(.*)$")


def normalize_description(content: str) -> str:
    """Collapse a multi-line frontmatter ``description`` into one quoted line.

    Generated frontmatter sometimes emits ``description`` as a YAML block
    scalar (``description: >`` / ``|``) or a wrapped value spanning several
    physical lines. That is valid YAML but breaks naive line-based plugin
    loaders. This folds such a value into a single double-quoted line,
    leaving the body and all other frontmatter untouched. A description
    that is already one line is returned unchanged.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return content
    block = match.group(1)
    folded = _fold_description_block(block)
    if folded == block:
        return content
    return content[: match.start(1)] + folded + content[match.end(1) :]


def _fold_description_block(block: str) -> str:
    lines = block.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _DESCRIPTION_KEY_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue

        indent, inline = m.group(1), m.group(2).strip()
        is_block_scalar = inline.startswith((">", "|"))
        is_empty_inline = inline == ""
        if not (is_block_scalar or is_empty_inline):
            # A normal single-line scalar — leave it exactly as written.
            out.append(line)
            i += 1
            continue

        key_indent = len(indent)
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            cont = lines[j]
            if cont.strip() == "":
                body.append("")
                j += 1
                continue
            cont_indent = len(cont) - len(cont.lstrip())
            if cont_indent <= key_indent:
                break
            body.append(cont.strip())
            j += 1

        folded_text = " ".join(part for part in body if part)
        if not folded_text:
            # Genuinely empty description with no continuation — don't touch it.
            out.append(line)
            i += 1
            continue

        out.append(f"{indent}description: {_yaml_double_quote(folded_text)}")
        i = j
    return "\n".join(out)


def _description_is_multiline(raw: str) -> bool:
    lines = raw.splitlines()
    for idx, line in enumerate(lines):
        m = _DESCRIPTION_KEY_RE.match(line)
        if not m:
            continue
        inline = m.group(2).strip()
        if inline.startswith((">", "|")):
            return True
        if inline == "":
            indent = len(m.group(1))
            nxt = idx + 1
            while nxt < len(lines) and lines[nxt].strip() == "":
                nxt += 1
            if nxt < len(lines):
                cont_indent = len(lines[nxt]) - len(lines[nxt].lstrip())
                return cont_indent > indent
        return False
    return False


def _yaml_double_quote(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    escaped = collapsed.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
