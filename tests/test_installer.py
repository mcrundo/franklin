"""Tests for franklin.installer (franklin install)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from franklin.installer import (
    InstallError,
    default_marketplace_root,
    install_plugin,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plugin(
    root: Path,
    *,
    name: str = "layered-rails",
    version: str = "0.1.0",
    description: str = "Test plugin",
    keywords: list[str] | None = None,
    include_manifest: bool = True,
    manifest_override: dict | None = None,
) -> Path:
    """Build a minimal plugin tree on disk and return its root."""
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / "skills" / name).mkdir(parents=True)
    (plugin_dir / "commands").mkdir()
    (plugin_dir / "agents").mkdir()

    (plugin_dir / "skills" / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill\n---\n# {name}\n"
    )
    (plugin_dir / "commands" / "do-thing.md").write_text(
        "---\ndescription: do a thing\n---\n# Do Thing\n"
    )

    if include_manifest:
        manifest = manifest_override or {
            "name": name,
            "version": version,
            "description": description,
            "license": "MIT",
        }
        if keywords:
            manifest["keywords"] = keywords
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2))

    return plugin_dir


# ---------------------------------------------------------------------------
# default_marketplace_root
# ---------------------------------------------------------------------------


def test_default_marketplace_root_honors_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "custom"
    monkeypatch.setenv("FRANKLIN_MARKETPLACE_DIR", str(override))
    assert default_marketplace_root() == override


def test_default_marketplace_root_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRANKLIN_MARKETPLACE_DIR", raising=False)
    result = default_marketplace_root()
    assert result.name == "marketplace"
    assert result.parent.name == ".franklin"


# ---------------------------------------------------------------------------
# install_plugin happy path
# ---------------------------------------------------------------------------


def test_install_plugin_fresh_install_copies_tree_and_writes_marketplace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin_root = _write_plugin(source, keywords=["rails", "architecture"])
    marketplace = tmp_path / "marketplace"

    result = install_plugin(plugin_root, marketplace_root=marketplace)

    assert result.plugin_name == "layered-rails"
    assert result.plugin_version == "0.1.0"
    assert result.replaced is False
    assert result.marketplace_root == marketplace
    assert result.plugin_root == marketplace / "layered-rails"

    # Plugin tree was copied
    assert (marketplace / "layered-rails" / ".claude-plugin" / "plugin.json").exists()
    assert (marketplace / "layered-rails" / "skills" / "layered-rails" / "SKILL.md").exists()
    assert (marketplace / "layered-rails" / "commands" / "do-thing.md").exists()

    # Marketplace manifest was written with the plugin registered
    manifest_path = marketplace / ".claude-plugin" / "marketplace.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["name"] == "franklin"
    assert isinstance(manifest["plugins"], list)
    assert len(manifest["plugins"]) == 1
    entry = manifest["plugins"][0]
    assert entry["name"] == "layered-rails"
    assert entry["source"] == "./layered-rails"
    assert entry["version"] == "0.1.0"
    assert entry["description"] == "Test plugin"
    assert entry["tags"] == ["rails", "architecture"]


def test_install_plugin_second_install_merges_into_same_marketplace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin_a = _write_plugin(source, name="plugin-a")
    plugin_b = _write_plugin(source, name="plugin-b")
    marketplace = tmp_path / "marketplace"

    install_plugin(plugin_a, marketplace_root=marketplace)
    install_plugin(plugin_b, marketplace_root=marketplace)

    # Both plugin trees present
    assert (marketplace / "plugin-a" / ".claude-plugin" / "plugin.json").exists()
    assert (marketplace / "plugin-b" / ".claude-plugin" / "plugin.json").exists()

    # Both registered in marketplace manifest, sorted by name
    manifest = json.loads((marketplace / ".claude-plugin" / "marketplace.json").read_text())
    names = [p["name"] for p in manifest["plugins"]]
    assert names == ["plugin-a", "plugin-b"]


def test_install_plugin_refuses_overwrite_without_force(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin_root = _write_plugin(source)
    marketplace = tmp_path / "marketplace"

    install_plugin(plugin_root, marketplace_root=marketplace)

    with pytest.raises(InstallError, match="already installed"):
        install_plugin(plugin_root, marketplace_root=marketplace)


def test_install_plugin_force_replaces_existing(tmp_path: Path) -> None:
    source_v1 = tmp_path / "source-v1"
    source_v1.mkdir()
    _write_plugin(source_v1, version="0.1.0")

    marketplace = tmp_path / "marketplace"
    install_plugin(source_v1 / "layered-rails", marketplace_root=marketplace)

    # Build a fresh v2 in a different source dir
    source_v2 = tmp_path / "source-v2"
    source_v2.mkdir()
    _write_plugin(source_v2, version="0.2.0", description="Updated")

    result = install_plugin(source_v2 / "layered-rails", marketplace_root=marketplace, force=True)
    assert result.replaced is True
    assert result.plugin_version == "0.2.0"

    # Marketplace manifest shows the updated version, only one entry
    manifest = json.loads((marketplace / ".claude-plugin" / "marketplace.json").read_text())
    assert len(manifest["plugins"]) == 1
    assert manifest["plugins"][0]["version"] == "0.2.0"
    assert manifest["plugins"][0]["description"] == "Updated"


def test_install_plugin_force_preserves_sibling_plugins(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_plugin(source, name="plugin-a")
    _write_plugin(source, name="plugin-b")

    marketplace = tmp_path / "marketplace"
    install_plugin(source / "plugin-a", marketplace_root=marketplace)
    install_plugin(source / "plugin-b", marketplace_root=marketplace)

    # Re-install plugin-a with force — plugin-b must still be there
    install_plugin(source / "plugin-a", marketplace_root=marketplace, force=True)

    assert (marketplace / "plugin-a" / ".claude-plugin" / "plugin.json").exists()
    assert (marketplace / "plugin-b" / ".claude-plugin" / "plugin.json").exists()
    manifest = json.loads((marketplace / ".claude-plugin" / "marketplace.json").read_text())
    assert sorted(p["name"] for p in manifest["plugins"]) == ["plugin-a", "plugin-b"]


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_install_plugin_errors_when_root_missing(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match="plugin root does not exist"):
        install_plugin(tmp_path / "nope", marketplace_root=tmp_path / "marketplace")


def test_install_plugin_errors_when_plugin_json_missing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin_root = _write_plugin(source, include_manifest=False)
    with pytest.raises(InstallError, match=r"no plugin\.json"):
        install_plugin(plugin_root, marketplace_root=tmp_path / "marketplace")


def test_install_plugin_errors_on_invalid_json(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin_root = _write_plugin(source, include_manifest=False)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text("{not json")
    with pytest.raises(InstallError, match="not valid JSON"):
        install_plugin(plugin_root, marketplace_root=tmp_path / "marketplace")


def test_install_plugin_errors_on_missing_name_field(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin_root = _write_plugin(
        source,
        manifest_override={"version": "0.1.0", "description": "no name"},
    )
    with pytest.raises(InstallError, match="missing required field 'name'"):
        install_plugin(plugin_root, marketplace_root=tmp_path / "marketplace")


def test_install_plugin_errors_on_plugin_json_that_is_not_an_object(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin_root = _write_plugin(source, include_manifest=False)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text("[1, 2, 3]")
    with pytest.raises(InstallError, match="must be a JSON object"):
        install_plugin(plugin_root, marketplace_root=tmp_path / "marketplace")


def test_install_plugin_errors_on_unsupported_platform(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin_root = _write_plugin(source)
    marketplace = tmp_path / "marketplace"

    with (
        patch("franklin.installer.sys.platform", "win32"),
        pytest.raises(InstallError, match="macOS and Linux only"),
    ):
        install_plugin(plugin_root, marketplace_root=marketplace)


def test_install_plugin_errors_on_corrupt_existing_marketplace_json(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin_root = _write_plugin(source)
    marketplace = tmp_path / "marketplace"
    (marketplace / ".claude-plugin").mkdir(parents=True)
    (marketplace / ".claude-plugin" / "marketplace.json").write_text("{not json")

    with pytest.raises(InstallError, match=r"existing marketplace\.json"):
        install_plugin(plugin_root, marketplace_root=marketplace)
