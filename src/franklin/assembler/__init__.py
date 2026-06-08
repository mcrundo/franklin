"""Stage 5: assemble the generated plugin tree.

Validates links and frontmatter, writes the .claude-plugin/plugin.json
manifest, and optionally packages the tree as a distributable archive.
Pure Python — no LLM calls. Fast enough to run repeatedly while iterating
on any earlier stage.
"""

from franklin.assembler.citations import CitationMismatch, check_citations
from franklin.assembler.frontmatter import (
    FrontmatterIssue,
    normalize_description,
    validate_frontmatter,
)
from franklin.assembler.links import (
    BrokenLink,
    relpath_from,
    rewrite_root_relative_links,
    validate_links,
)
from franklin.assembler.lint import LintFinding, has_errors, lint_plugin
from franklin.assembler.manifest import write_plugin_manifest
from franklin.assembler.packager import package_plugin
from franklin.assembler.readme import generate_readme
from franklin.assembler.templates import TemplateLeak, find_template_leaks

__all__ = [
    "BrokenLink",
    "CitationMismatch",
    "FrontmatterIssue",
    "LintFinding",
    "TemplateLeak",
    "check_citations",
    "find_template_leaks",
    "generate_readme",
    "has_errors",
    "lint_plugin",
    "normalize_description",
    "package_plugin",
    "relpath_from",
    "rewrite_root_relative_links",
    "validate_frontmatter",
    "validate_links",
    "write_plugin_manifest",
]
