"""Tests for the assemble stage."""

from __future__ import annotations

import json
from pathlib import Path

from franklin.assembler import validate_links, write_plugin_manifest
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


# ---------------------------------------------------------------------------
# Link validator tests
# ---------------------------------------------------------------------------


def _mkplugin(tmp_path: Path) -> Path:
    """Build a small plugin tree mirroring the real layout."""
    root = tmp_path / "plugin"
    (root / "skills/p/references/patterns").mkdir(parents=True)
    (root / "skills/p/references/core").mkdir(parents=True)
    (root / "commands").mkdir(parents=True)
    (root / "agents").mkdir(parents=True)

    (root / "skills/p/SKILL.md").write_text("# Skill\n")
    (root / "skills/p/references/patterns/service-objects.md").write_text("# SO\n")
    (root / "skills/p/references/core/layered-architecture.md").write_text("# LA\n")
    (root / "commands/spec-test.md").write_text("# spec\n")
    (root / "agents/reviewer.md").write_text("# reviewer\n")
    return root


def test_validate_links_returns_empty_when_all_links_resolve(tmp_path: Path) -> None:
    root = _mkplugin(tmp_path)
    # Add a file with only valid links
    (root / "skills/p/references/patterns/query-objects.md").write_text(
        "# Query Objects\n"
        "See [service objects](service-objects.md).\n"
        "Also [layered architecture](../core/layered-architecture.md).\n"
        "And [spec test](../../../../commands/spec-test.md).\n"
    )
    assert validate_links(root) == []


def test_validate_links_flags_invented_paths(tmp_path: Path) -> None:
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/query-objects.md").write_text(
        "# Query Objects\n"
        "See [missing](../nonexistent/file.md).\n"
        "And [bad command](../../../commands/spec-test.md).\n"  # off-by-one depth
    )
    broken = validate_links(root)
    assert len(broken) == 2
    paths = sorted(b.target_path for b in broken)
    assert paths == ["../../../commands/spec-test.md", "../nonexistent/file.md"]


def test_validate_links_ignores_external_urls_and_anchors(tmp_path: Path) -> None:
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/query-objects.md").write_text(
        "# Query Objects\n"
        "- [anthropic](https://anthropic.com)\n"
        "- [email](mailto:hi@example.com)\n"
        "- [section](#overview)\n"
    )
    assert validate_links(root) == []


def test_validate_links_strips_fragments_before_checking(tmp_path: Path) -> None:
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/query-objects.md").write_text(
        "# Query Objects\n"
        "See [service objects](service-objects.md#when-to-use).\n"
    )
    assert validate_links(root) == []


def test_validate_links_reports_line_numbers(tmp_path: Path) -> None:
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/query-objects.md").write_text(
        "# Query Objects\n"
        "\n"
        "Body text.\n"
        "\n"
        "[broken](missing.md)\n"
    )
    broken = validate_links(root)
    assert len(broken) == 1
    assert broken[0].line_number == 5
    assert broken[0].target_path == "missing.md"
