"""Tests for the assemble stage."""

from __future__ import annotations

import json
from pathlib import Path

from franklin.assembler import write_plugin_manifest
from franklin.schema import PluginMeta


def test_write_plugin_manifest_creates_claude_plugin_directory(tmp_path: Path) -> None:
    plugin_root = tmp_path / "layered-rails"
    meta = PluginMeta(
        name="layered-rails",
        version="0.1.0",
        description="Test plugin",
        keywords=["rails", "architecture"],
    )
    manifest_path = write_plugin_manifest(plugin_root, meta)

    assert manifest_path == plugin_root / ".claude-plugin" / "plugin.json"
    assert manifest_path.exists()
    assert (plugin_root / ".claude-plugin").is_dir()


def test_write_plugin_manifest_contains_expected_fields(tmp_path: Path) -> None:
    plugin_root = tmp_path / "layered-rails"
    meta = PluginMeta(
        name="layered-rails",
        version="0.1.0",
        description="Layered design for Rails apps",
        keywords=["rails", "architecture", "patterns"],
    )
    manifest_path = write_plugin_manifest(plugin_root, meta)

    data = json.loads(manifest_path.read_text())
    assert data["name"] == "layered-rails"
    assert data["version"] == "0.1.0"
    assert data["description"] == "Layered design for Rails apps"
    assert data["keywords"] == ["rails", "architecture", "patterns"]
    assert data["license"] == "MIT"


def test_write_plugin_manifest_omits_empty_keywords(tmp_path: Path) -> None:
    plugin_root = tmp_path / "x"
    meta = PluginMeta(name="x", version="0.1.0", description="d")
    manifest_path = write_plugin_manifest(plugin_root, meta)

    data = json.loads(manifest_path.read_text())
    assert "keywords" not in data


def test_write_plugin_manifest_is_idempotent(tmp_path: Path) -> None:
    """Re-running assemble should overwrite cleanly without errors."""
    plugin_root = tmp_path / "x"
    meta = PluginMeta(name="x", version="0.1.0", description="d")

    write_plugin_manifest(plugin_root, meta)
    # Second call should not raise and should produce the same content
    path2 = write_plugin_manifest(plugin_root, meta)
    assert json.loads(path2.read_text())["name"] == "x"
