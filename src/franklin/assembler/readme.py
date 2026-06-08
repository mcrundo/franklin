"""Generate a README.md for the assembled plugin.

Produces a GitHub-ready README from the plan metadata and output tree,
matching the style of published Claude Code plugin repos like
palkan/skills and mcrundo/sustainable-rails-skill. No LLM calls — this
is deterministic assembly from the plan + book metadata.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from franklin.schema import ArtifactType, BookManifest, PlanManifest

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Maximum length for a one-line table/list summary before we cap on a word
# boundary. Generous enough that almost every authored description fits whole.
_SUMMARY_CAP = 160


def _command_summary(plugin_root: Path, artifact_path: str, fallback: str) -> str:
    """Return a one-line summary for a command's README table row.

    Prefers the command file's authoritative frontmatter ``description``
    (a concise single sentence written by the reduce stage) over the
    plan-stage ``brief``, so the README never paraphrases or truncates
    mid-word. Falls back to the brief only when the file has no usable
    description.
    """
    description = _read_frontmatter_description(plugin_root / artifact_path)
    text = description if description else fallback
    return _cap_on_word_boundary(" ".join(text.split()))


def _brief_summary(text: str) -> str:
    """Return the first sentence of a plan brief, cleaned and length-capped.

    Used for agent bullets, where the file's frontmatter ``description``
    is a verbose "Use this agent when…" trigger string unsuited to a
    one-line summary. Splits on a sentence boundary only — never on a
    colon — to avoid severing a list-introducing clause.
    """
    text = " ".join(text.split())
    if ". " in text:
        text = text[: text.index(". ") + 1]
    return _cap_on_word_boundary(text)


def _read_frontmatter_description(md_file: Path) -> str | None:
    try:
        match = _FRONTMATTER_RE.match(md_file.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not match:
        return None
    try:
        data: Any = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if isinstance(data, dict):
        desc = data.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()
    return None


def _cap_on_word_boundary(text: str, limit: int = _SUMMARY_CAP) -> str:
    """Trim to ``limit`` chars on a word boundary, only if genuinely over.

    Never emits a dangling mid-word ``...``; appends a single ellipsis
    character on a clean word boundary when (and only when) truncation
    actually happens.
    """
    if len(text) <= limit:
        return text
    cut = text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return f"{cut}…"


def generate_readme(
    plugin_root: Path,
    *,
    plan: PlanManifest,
    book: BookManifest,
    repo: str | None = None,
) -> Path:
    """Write a README.md to ``plugin_root`` and return the path."""
    plugin = plan.plugin
    authors = ", ".join(book.metadata.authors) or "Unknown"

    lines: list[str] = []

    # ---- header -----------------------------------------------------------

    lines.append(f"# {plugin.name}")
    lines.append("")
    lines.append(
        f"A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin "
        f"based on *{book.metadata.title}* by {authors}."
    )
    lines.append("")

    if plugin.description:
        lines.append(plugin.description)
        lines.append("")

    # ---- install ----------------------------------------------------------

    lines.append("## Install")
    lines.append("")
    if repo:
        lines.append("```bash")
        lines.append(f"claude plugin marketplace add {repo}")
        lines.append(f"claude plugin install {plugin.name}@{plugin.name}")
        lines.append("```")
    else:
        lines.append("```bash")
        lines.append("claude plugin marketplace add owner/repo")
        lines.append(f"claude plugin install {plugin.name}@{plugin.name}")
        lines.append("```")
        lines.append("")
        lines.append(
            "*Replace `owner/repo` with the GitHub repository "
            "after publishing with `franklin push`.*"
        )
    lines.append("")

    # ---- commands ---------------------------------------------------------

    commands = [a for a in plan.artifacts if a.type == ArtifactType.COMMAND]
    if commands:
        lines.append("## Commands")
        lines.append("")
        lines.append("| Command | Purpose |")
        lines.append("|---------|---------|")
        for cmd in commands:
            cmd_name = Path(cmd.path).stem
            summary = _command_summary(plugin_root, cmd.path, cmd.brief)
            lines.append(f"| `/{plugin.name}:{cmd_name}` | {summary} |")
        lines.append("")

    # ---- agents -----------------------------------------------------------

    agents = [a for a in plan.artifacts if a.type == ArtifactType.AGENT]
    if agents:
        lines.append("## Agents")
        lines.append("")
        for agent in agents:
            agent_name = Path(agent.path).stem
            lines.append(f"- **{agent_name}** — {_brief_summary(agent.brief)}")
        lines.append("")

    # ---- reference files --------------------------------------------------

    references = [a for a in plan.artifacts if a.type == ArtifactType.REFERENCE]
    if references:
        lines.append("## Reference Files")
        lines.append("")
        lines.append(f"{len(references)} reference files organized into:")
        lines.append("")

        # Group by directory under references/
        groups: dict[str, list[str]] = {}
        for ref in references:
            parts = Path(ref.path).parts
            # Find the 'references' segment and take the next part as group
            try:
                ref_idx = list(parts).index("references")
                group = parts[ref_idx + 1] if ref_idx + 1 < len(parts) - 1 else "general"
            except ValueError:
                group = "general"
            groups.setdefault(group, []).append(Path(ref.path).stem)

        for group, files in sorted(groups.items()):
            file_list = ", ".join(sorted(files))
            lines.append(f"- `references/{group}/` — {file_list}")
        lines.append("")

    # ---- generated by -----------------------------------------------------

    lines.append("---")
    lines.append("")
    lines.append(
        "Generated by [Franklin](https://github.com/mcrundo/franklin) "
        f"from *{book.metadata.title}*."
    )
    lines.append("")

    readme_path = plugin_root / "README.md"
    readme_path.write_text("\n".join(lines))
    return readme_path
