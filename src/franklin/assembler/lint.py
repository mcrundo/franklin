"""Deterministic assemble-time lint gate for a generated plugin tree.

Aggregates every pure-Python check that can catch a generation defect
before a plugin ships, and classifies each finding as a build-failing
``error`` or an advisory ``warning``. This is the single gate the
project's plugin-generation quality work converges on: instead of six
separate post-hoc cleanups, one ``lint_plugin`` pass guards them all.

Composes the existing validators (links, template leaks, frontmatter)
and adds: README command-table truncation, plugin-manifest required
fields, heuristic citation-integrity (warning), and — in ``bundle``
mode — publish-bundle placeholder/author checks.

Pure Python, no LLM calls.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from franklin.assembler.citations import check_citations
from franklin.assembler.frontmatter import validate_frontmatter
from franklin.assembler.links import validate_links
from franklin.assembler.templates import find_template_leaks

# Required string fields in .claude-plugin/plugin.json. `author` is resolved at
# publish time (franklin push), so it's only required in bundle mode.
_REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = ("name", "version", "description")

# A markdown table cell that ends in a dangling ellipsis — the signature of a
# truncated/paraphrased description.
_TRUNCATED_CELL_RE = re.compile(r"(\.\.\.|…)\s*\|")


@dataclass(frozen=True)
class LintFinding:
    """One lint result. ``severity`` is ``"error"`` or ``"warning"``."""

    check: str
    severity: str
    source_file: Path
    message: str
    line_number: int | None = None


def lint_plugin(plugin_root: Path, *, bundle: bool = False) -> list[LintFinding]:
    """Run every deterministic check over ``plugin_root``.

    With ``bundle=False`` (default) this lints a freshly assembled plugin —
    the ``owner/repo`` README placeholder is expected pre-publish and is NOT
    flagged. With ``bundle=True`` it lints a published marketplace bundle and
    additionally requires the README placeholder to have been substituted and
    ``plugin.json`` to carry an ``author``.
    """
    findings: list[LintFinding] = []
    findings.extend(_lint_links(plugin_root))
    findings.extend(_lint_template_leaks(plugin_root))
    findings.extend(_lint_frontmatter(plugin_root))
    findings.extend(_lint_readme_truncation(plugin_root))
    findings.extend(_lint_manifest(plugin_root, bundle=bundle))
    findings.extend(_lint_citations(plugin_root))
    if bundle:
        findings.extend(_lint_bundle_placeholders(plugin_root))
    return findings


def has_errors(findings: list[LintFinding]) -> bool:
    return any(f.severity == "error" for f in findings)


def _lint_links(plugin_root: Path) -> list[LintFinding]:
    out: list[LintFinding] = []
    for link in validate_links(plugin_root):
        check = "broken_link" if link.kind == "missing" else "placeholder_link"
        out.append(
            LintFinding(
                check=check,
                severity="error",
                source_file=link.source_file,
                line_number=link.line_number,
                message=f"{link.kind} link target {link.target_path!r}",
            )
        )
    return out


def _lint_template_leaks(plugin_root: Path) -> list[LintFinding]:
    return [
        LintFinding(
            check="template_leak",
            severity="error",
            source_file=leak.source_file,
            line_number=leak.line_number,
            message=f"unfilled template token {leak.placeholder!r}",
        )
        for leak in find_template_leaks(plugin_root)
    ]


def _lint_frontmatter(plugin_root: Path) -> list[LintFinding]:
    return [
        LintFinding(
            check="frontmatter",
            severity="error",
            source_file=issue.source_file,
            message=f"{issue.kind}: {issue.message}",
        )
        for issue in validate_frontmatter(plugin_root)
    ]


def _lint_readme_truncation(plugin_root: Path) -> list[LintFinding]:
    readme = plugin_root / "README.md"
    if not readme.exists():
        return []
    out: list[LintFinding] = []
    for line_number, line in enumerate(readme.read_text(encoding="utf-8").splitlines(), start=1):
        if _TRUNCATED_CELL_RE.search(line):
            out.append(
                LintFinding(
                    check="readme_truncation",
                    severity="error",
                    source_file=readme,
                    line_number=line_number,
                    message="README table cell ends in a dangling ellipsis",
                )
            )
    return out


def _lint_manifest(plugin_root: Path, *, bundle: bool) -> list[LintFinding]:
    manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        return [
            LintFinding(
                check="manifest_field",
                severity="error",
                source_file=manifest_path,
                message="missing .claude-plugin/plugin.json",
            )
        ]
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [
            LintFinding(
                check="manifest_field",
                severity="error",
                source_file=manifest_path,
                message=f"plugin.json is not valid JSON: {exc.msg}",
            )
        ]

    out: list[LintFinding] = []
    required = _REQUIRED_MANIFEST_FIELDS + (("author",) if bundle else ())
    for field in required:
        value = data.get(field) if isinstance(data, dict) else None
        if field == "author":
            ok = isinstance(value, dict) and bool(value.get("name"))
        else:
            ok = isinstance(value, str) and bool(value.strip())
        if not ok:
            out.append(
                LintFinding(
                    check="manifest_field",
                    severity="error",
                    source_file=manifest_path,
                    message=f"plugin.json missing or empty required field {field!r}",
                )
            )
    return out


def _lint_citations(plugin_root: Path) -> list[LintFinding]:
    return [
        LintFinding(
            check="citation_mismatch",
            severity="warning",
            source_file=mismatch.source_file,
            line_number=mismatch.line_number,
            message=(
                f"prose cites Chapter {mismatch.prose_chapter} but this file's "
                f"_source tags reference {list(mismatch.cited_chapters)}"
            ),
        )
        for mismatch in check_citations(plugin_root)
    ]


def _lint_bundle_placeholders(plugin_root: Path) -> list[LintFinding]:
    readme = plugin_root / "README.md"
    if not readme.exists():
        return []
    out: list[LintFinding] = []
    text = readme.read_text(encoding="utf-8")
    if "owner/repo" in text or "Replace `owner/repo`" in text:
        out.append(
            LintFinding(
                check="default_placeholder",
                severity="error",
                source_file=readme,
                message="published README still contains the 'owner/repo' placeholder",
            )
        )
    return out
