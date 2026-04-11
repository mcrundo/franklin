"""Stage 5: assemble the generated plugin tree.

Validates links and frontmatter, writes the .claude-plugin/plugin.json
manifest, and optionally packages the tree as a distributable archive.
Pure Python — no LLM calls. Fast enough to run repeatedly while iterating
on any earlier stage.
"""

from franklin.assembler.manifest import write_plugin_manifest

__all__ = ["write_plugin_manifest"]
