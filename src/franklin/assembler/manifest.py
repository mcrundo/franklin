"""Write the .claude-plugin/plugin.json manifest for a generated plugin tree."""

from __future__ import annotations

import json
import os
import subprocess
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
        "author": _default_author(),
        "license": "MIT",
    }
    if meta.keywords:
        manifest["keywords"] = list(meta.keywords)

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest_path


def _default_author() -> dict[str, str]:
    """Return author metadata for generated plugin manifests.

    Franklin can run without GitHub context during assemble, so use
    explicit env vars first, then local git config, and finally a stable
    generator identity. The publish step may still use the GitHub repo
    owner for marketplace metadata.
    """
    name = os.environ.get("FRANKLIN_PLUGIN_AUTHOR_NAME", "").strip()
    email = os.environ.get("FRANKLIN_PLUGIN_AUTHOR_EMAIL", "").strip()

    if not name:
        name = _git_config("user.name")
    if not email:
        email = _git_config("user.email")

    author = {"name": name or "franklin"}
    if email:
        author["email"] = email
    return author


def _git_config(key: str) -> str:
    try:
        result = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
