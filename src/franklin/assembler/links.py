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
    """One relative link whose target file does not exist."""

    source_file: Path
    line_number: int
    link_text: str
    target_path: str
    resolved_path: Path


def validate_links(plugin_root: Path) -> list[BrokenLink]:
    """Walk every .md file under plugin_root and return broken relative links.

    Checks only relative file links — anything that looks like a URL scheme
    (http://, mailto:, etc.) or a same-file anchor (#section) is skipped.
    Fragments on otherwise-valid paths (`file.md#anchor`) are stripped before
    resolving the file.
    """
    broken: list[BrokenLink] = []
    for md_file in sorted(plugin_root.rglob("*.md")):
        broken.extend(_validate_file(md_file))
    return broken


def _validate_file(md_file: Path) -> list[BrokenLink]:
    broken: list[BrokenLink] = []
    text = md_file.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in _INLINE_LINK.finditer(line):
            link_text = match.group(1)
            target = match.group(2).strip()

            if _is_external_or_same_file_anchor(target):
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
                    )
                )
    return broken


def _is_external_or_same_file_anchor(target: str) -> bool:
    return target.startswith(_EXTERNAL_PREFIXES) or target.startswith("#")
