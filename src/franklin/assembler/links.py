"""Markdown link validator for generated plugin trees.

Walks every `.md` file under the plugin root, parses inline markdown links
(`[text](path)`), and returns any relative file links whose targets do not
exist. Skips external URLs, mailto: links, and same-file anchors — those
aren't validatable from disk alone.

Pure Python, no LLM calls. Fast enough to run every time `franklin
assemble` is invoked, which makes it the belt-and-suspenders check for
the "generator invented a path" failure mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Matches inline markdown links [text](target). Does not match reference-style
# [text][label] links or image ![alt](src) links — those are rare in generated
# plugin content and not worth the regex complexity for v1.
_INLINE_LINK = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)")

_EXTERNAL_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "mailto:",
    "ftp://",
    "ftps://",
    "tel:",
)


@dataclass(frozen=True)
class BrokenLink:
    """One relative link that is either missing or an unfilled placeholder.

    `kind` distinguishes:
    - "missing" — a well-formed relative path whose target file does not exist
    - "placeholder" — a target that looks like an unfilled template slot
      (contains `<...>`, `{{...}}`, or similar), so the model forgot to
      substitute a real value before emitting the link
    """

    source_file: Path
    line_number: int
    link_text: str
    target_path: str
    resolved_path: Path
    kind: str = "missing"


def validate_links(plugin_root: Path) -> list[BrokenLink]:
    """Walk every .md file under plugin_root and return broken relative links.

    Checks only relative file links — anything that looks like a URL scheme
    (http://, mailto:, etc.) or a same-file anchor (#section) is skipped.
    Fragments on otherwise-valid paths (`file.md#anchor`) are stripped before
    resolving the file.

    Targets that contain placeholder syntax (`<...>`, `{{...}}`) are reported
    with `kind="placeholder"` without resolving the path, so callers can
    distinguish "generator leaked a template slot" from "generator invented
    a real-looking path that doesn't exist."
    """
    broken: list[BrokenLink] = []
    for md_file in sorted(plugin_root.rglob("*.md")):
        broken.extend(_validate_file(md_file))
    return broken


def repair_common_agent_links(plugin_root: Path) -> None:
    """Rewrite common plugin-root-relative links inside agent files.

    Agents live in ``agents/``. LLMs sometimes emit links as if they were
    rooted at the plugin directory, such as ``commands/foo.md`` or
    ``references/core/foo.md``. Those paths are wrong from ``agents/``
    but have unambiguous canonical forms in Franklin plugin trees.
    """
    agents_dir = plugin_root / "agents"
    if not agents_dir.is_dir():
        return
    skill_name = _primary_skill_name(plugin_root)
    for agent_md in sorted(agents_dir.glob("*.md")):
        text = agent_md.read_text(encoding="utf-8")
        repaired = _INLINE_LINK.sub(
            lambda match: _repair_agent_link(match, plugin_root, skill_name),
            text,
        )
        if repaired != text:
            agent_md.write_text(repaired, encoding="utf-8")


def _validate_file(md_file: Path) -> list[BrokenLink]:
    broken: list[BrokenLink] = []
    text = md_file.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in _INLINE_LINK.finditer(line):
            link_text = match.group(1)
            target = match.group(2).strip()

            if _is_external_or_same_file_anchor(target):
                continue

            if _looks_like_placeholder(target):
                broken.append(
                    BrokenLink(
                        source_file=md_file,
                        line_number=line_number,
                        link_text=link_text,
                        target_path=target,
                        resolved_path=md_file.parent / target,
                        kind="placeholder",
                    )
                )
                continue

            # Strip URL fragment (e.g. "path/to/file.md#section" -> "path/to/file.md")
            path_only = target.split("#", maxsplit=1)[0].strip()
            if not path_only:
                continue

            # Resolve relative to the directory containing the source file.
            resolved = (md_file.parent / path_only).resolve()
            if not resolved.exists():
                broken.append(
                    BrokenLink(
                        source_file=md_file,
                        line_number=line_number,
                        link_text=link_text,
                        target_path=target,
                        resolved_path=resolved,
                        kind="missing",
                    )
                )
    return broken


def _is_external_or_same_file_anchor(target: str) -> bool:
    return target.startswith(_EXTERNAL_PREFIXES) or target.startswith("#")


def _looks_like_placeholder(target: str) -> bool:
    """Detect unfilled template placeholders in a link target.

    Flags:
    - `{{...}}` — Franklin's own template variable syntax
    - `<...>` with any angle bracket character — descriptive placeholders
      like `<relative path to reference>` or `<command name>`

    Scoped to link targets only; the general `find_template_leaks` scanner
    handles body-text leaks where angle brackets might be legitimate prose.
    """
    return "{{" in target or "}}" in target or "<" in target or ">" in target


def _primary_skill_name(plugin_root: Path) -> str:
    skills_dir = plugin_root / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            if child.is_dir():
                return child.name
    return plugin_root.name


def _repair_agent_link(match: re.Match[str], plugin_root: Path, skill_name: str) -> str:
    link_text = match.group(1)
    target = match.group(2).strip()
    if _is_external_or_same_file_anchor(target) or _looks_like_placeholder(target):
        return match.group(0)

    fragment = ""
    path_only = target
    if "#" in target:
        path_only, fragment = target.split("#", maxsplit=1)
        fragment = f"#{fragment}"

    replacement: str | None = None
    if path_only.startswith("commands/"):
        candidate = f"../{path_only}"
        if (plugin_root / path_only).exists():
            replacement = candidate
    elif path_only.startswith("references/"):
        candidate = f"../skills/{skill_name}/{path_only}"
        if (plugin_root / "skills" / skill_name / path_only).exists():
            replacement = candidate
    elif path_only.startswith(f"skills/{skill_name}/"):
        candidate = f"../{path_only}"
        if (plugin_root / path_only).exists():
            replacement = candidate

    if replacement is None:
        return match.group(0)
    return f"[{link_text}]({replacement}{fragment})"
