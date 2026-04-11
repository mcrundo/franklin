"""Write the .claude-plugin/plugin.json manifest for a generated plugin tree."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from franklin.schema import PluginMeta


def write_plugin_manifest(plugin_root: Path, meta: PluginMeta) -> Path:
    """Write `<plugin_root>/.claude-plugin/plugin.json` from a PluginMeta.

    Returns the path the manifest was written to. Creates the
    `.claude-plugin` directory if it doesn't exist.
    """
    manifest_dir = plugin_root / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "plugin.json"

    manifest: dict[str, Any] = {
        "name": meta.name,
        "version": meta.version,
        "description": meta.description,
        "license": "MIT",
    }
    if meta.keywords:
        manifest["keywords"] = list(meta.keywords)

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest_path
