"""Stage 5: assemble the generated plugin tree.

Validates links and frontmatter, writes the .claude-plugin/plugin.json
manifest, and optionally packages the tree as a distributable archive.
Pure Python — no LLM calls. Fast enough to run repeatedly while iterating
on any earlier stage.
"""

from franklin.assembler.frontmatter import FrontmatterIssue, validate_frontmatter
from franklin.assembler.links import BrokenLink, validate_links
from franklin.assembler.manifest import write_plugin_manifest
from franklin.assembler.templates import TemplateLeak, find_template_leaks

__all__ = [
    "BrokenLink",
    "FrontmatterIssue",
    "TemplateLeak",
    "find_template_leaks",
    "validate_frontmatter",
    "validate_links",
    "write_plugin_manifest",
]
