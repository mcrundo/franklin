"""Detect unfilled Franklin template placeholders in generated files.

Franklin's prompt templates use `{{name}}` substitution. If a generator's
output contains `{{...}}` the model copied a template variable verbatim
instead of filling it in — a clean failure mode distinct from a generic
hallucination. This scanner walks every markdown file in the plugin tree
and returns every leak it finds.

Scoped to unambiguous `{{...}}` matches only. Angle-bracket placeholders
(`<relative path to reference>`) are caught separately by the link
validator when they appear inside link targets, where they can be
detected without false positives against legitimate HTML or prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_LEAK_PATTERN = re.compile(r"\{\{[^{}]+\}\}")


@dataclass(frozen=True)
class TemplateLeak:
    """One unfilled `{{name}}` placeholder found in a generated file."""

    source_file: Path
    line_number: int
    placeholder: str
    context: str


def find_template_leaks(plugin_root: Path) -> list[TemplateLeak]:
    """Return every unfilled `{{...}}` placeholder in the plugin tree."""
    leaks: list[TemplateLeak] = []
    for md_file in sorted(plugin_root.rglob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in _LEAK_PATTERN.finditer(line):
                leaks.append(
                    TemplateLeak(
                        source_file=md_file,
                        line_number=line_number,
                        placeholder=match.group(0),
                        context=line.strip()[:120],
                    )
                )
    return leaks
