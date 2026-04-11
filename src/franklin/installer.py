"""Install an assembled plugin tree into a local franklin-owned marketplace.

Claude Code's plugin system is marketplace-based: plugins live under
`<marketplace-root>/<plugin-name>/` and the marketplace exposes them via a
`.claude-plugin/marketplace.json` manifest at its root. Users then run
`/plugin marketplace add <path>` followed by `/plugin install <name>@<slug>`
to activate any plugin it contains.

Rather than writing into `~/.claude/plugins/` (which is Claude-managed and
unsafe for external tools to modify), franklin owns its own local
marketplace at `~/.franklin/marketplace/`. Each `franklin install` run
copies the assembled plugin tree into that marketplace and merges the
plugin's entry into the marketplace manifest. Every plugin installed this
way participates in the same synthetic `franklin` marketplace; users only
need to add the marketplace once, after which future installs are a
single `/plugin install` away.

macOS and Linux only. Windows plugin paths and permissions differ enough
that v1 refuses up front rather than silently producing a broken install.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKETPLACE_NAME = "franklin"
MARKETPLACE_DESCRIPTION = "Locally installed plugins built by franklin"
MARKETPLACE_DIR_ENV = "FRANKLIN_MARKETPLACE_DIR"


class InstallError(RuntimeError):
    """Raised when install preparation or execution fails."""


@dataclass(frozen=True)
class InstallResult:
    """Outcome of a successful install_plugin call."""

    plugin_name: str
    plugin_version: str
    marketplace_root: Path
    plugin_root: Path
    replaced: bool


def default_marketplace_root() -> Path:
    """Return the franklin-owned local marketplace directory.

    Honors ``FRANKLIN_MARKETPLACE_DIR`` for tests and power users who want
    a different location; otherwise defaults to ``~/.franklin/marketplace``.
    """
    import os

    override = os.environ.get(MARKETPLACE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".franklin" / "marketplace"


def install_plugin(
    plugin_root: Path,
    *,
    marketplace_root: Path | None = None,
    force: bool = False,
) -> InstallResult:
    """Copy ``plugin_root`` into the local franklin marketplace.

    Validates the plugin's ``.claude-plugin/plugin.json`` before touching
    any filesystem state. Refuses to overwrite an existing plugin of the
    same name in the marketplace unless ``force=True``. On success,
    rewrites ``marketplace_root/.claude-plugin/marketplace.json`` to
    reflect the installed plugin's entry (replacing any previous entry
    with the same name, preserving other franklin-installed plugins).
    """
    _require_supported_platform()

    if not plugin_root.is_dir():
        raise InstallError(f"plugin root does not exist: {plugin_root}")

    manifest = _load_plugin_manifest(plugin_root)
    plugin_name = str(manifest["name"])
    plugin_version = str(manifest.get("version", "0.0.0"))

    marketplace = marketplace_root or default_marketplace_root()
    destination = marketplace / plugin_name

    replaced = False
    if destination.exists():
        if not force:
            raise InstallError(
                f"{plugin_name!r} is already installed at {destination} — "
                "pass --force to overwrite"
            )
        shutil.rmtree(destination)
        replaced = True

    marketplace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(plugin_root, destination)

    _merge_marketplace_manifest(marketplace, manifest)

    return InstallResult(
        plugin_name=plugin_name,
        plugin_version=plugin_version,
        marketplace_root=marketplace,
        plugin_root=destination,
        replaced=replaced,
    )


# ---------------------------------------------------------------------------
# Platform guard
# ---------------------------------------------------------------------------


def _require_supported_platform() -> None:
    if sys.platform.startswith("win"):
        raise InstallError(
            "franklin install currently supports macOS and Linux only "
            "(Windows plugin paths and permissions need separate handling)"
        )


# ---------------------------------------------------------------------------
# plugin.json validation
# ---------------------------------------------------------------------------


def _load_plugin_manifest(plugin_root: Path) -> dict[str, Any]:
    manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        raise InstallError(
            f"no plugin.json at {manifest_path} — run `franklin assemble` to produce one"
        )

    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InstallError(f"plugin.json at {manifest_path} is not valid JSON: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise InstallError(
            f"plugin.json at {manifest_path} must be a JSON object (got {type(data).__name__})"
        )

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InstallError(f"plugin.json at {manifest_path} is missing required field 'name'")

    return data


# ---------------------------------------------------------------------------
# marketplace.json merge
# ---------------------------------------------------------------------------


def _merge_marketplace_manifest(marketplace_root: Path, plugin_manifest: dict[str, Any]) -> Path:
    """Add or update the current plugin's entry in marketplace.json."""
    manifest_dir = marketplace_root / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "marketplace.json"

    if manifest_path.exists():
        try:
            existing: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(
                f"existing marketplace.json at {manifest_path} is invalid: {exc.msg}"
            ) from exc
        if not isinstance(existing, dict):
            raise InstallError(
                f"existing marketplace.json at {manifest_path} must be a JSON object"
            )
    else:
        existing = _fresh_marketplace_manifest()

    plugins_field = existing.get("plugins")
    plugins: list[dict[str, Any]] = (
        [p for p in plugins_field if isinstance(p, dict)]
        if isinstance(plugins_field, list)
        else []
    )

    new_entry = _plugin_entry(plugin_manifest)
    plugins = [p for p in plugins if p.get("name") != new_entry["name"]]
    plugins.append(new_entry)
    plugins.sort(key=lambda p: str(p.get("name", "")))
    existing["plugins"] = plugins

    manifest_path.write_text(json.dumps(existing, indent=2) + "\n")
    return manifest_path


def _fresh_marketplace_manifest() -> dict[str, Any]:
    return {
        "name": MARKETPLACE_NAME,
        "owner": {"name": "franklin"},
        "metadata": {"description": MARKETPLACE_DESCRIPTION},
        "plugins": [],
    }


def _plugin_entry(manifest: dict[str, Any]) -> dict[str, Any]:
    name = str(manifest["name"])
    entry: dict[str, Any] = {
        "name": name,
        "source": f"./{name}",
    }
    for field in ("version", "description", "homepage"):
        value = manifest.get(field)
        if isinstance(value, str) and value.strip():
            entry[field] = value
    author = manifest.get("author")
    if isinstance(author, dict):
        entry["author"] = author
    keywords = manifest.get("keywords")
    if isinstance(keywords, list):
        tags = [k for k in keywords if isinstance(k, str)]
        if tags:
            entry["tags"] = tags
    return entry
