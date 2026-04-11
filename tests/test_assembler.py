"""Tests for the assemble stage."""

from __future__ import annotations

import json
from pathlib import Path

from franklin.assembler import (
    find_template_leaks,
    validate_frontmatter,
    validate_links,
    write_plugin_manifest,
)
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
    assert broken[0].kind == "missing"


def test_validate_links_flags_angle_bracket_placeholders_as_placeholder_kind(
    tmp_path: Path,
) -> None:
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/query-objects.md").write_text(
        "# Query Objects\n"
        "See [reference](<relative path to reference>).\n"
        "And [command](<command name>).\n"
    )
    broken = validate_links(root)
    assert len(broken) == 2
    assert all(b.kind == "placeholder" for b in broken)
    targets = sorted(b.target_path for b in broken)
    assert targets == ["<command name>", "<relative path to reference>"]


def test_validate_links_flags_double_brace_placeholders(tmp_path: Path) -> None:
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/query-objects.md").write_text(
        "[slipped template](../{{plan_tree_path}}.md)\n"
    )
    broken = validate_links(root)
    assert len(broken) == 1
    assert broken[0].kind == "placeholder"


# ---------------------------------------------------------------------------
# Template leak scanner tests
# ---------------------------------------------------------------------------


def test_find_template_leaks_detects_double_brace_placeholders(tmp_path: Path) -> None:
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/service-objects.md").write_text(
        "# Service Objects\n\nThe plugin is {{plugin_name}}.\n"
    )
    leaks = find_template_leaks(root)
    assert len(leaks) == 1
    assert leaks[0].placeholder == "{{plugin_name}}"
    assert leaks[0].line_number == 3


def test_find_template_leaks_returns_empty_on_clean_tree(tmp_path: Path) -> None:
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/service-objects.md").write_text(
        "# Service Objects\n\nRegular body text with no placeholders.\n"
    )
    assert find_template_leaks(root) == []


def test_find_template_leaks_ignores_single_braces(tmp_path: Path) -> None:
    """Code examples often contain literal { and } — don't false-positive on them."""
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/service-objects.md").write_text(
        "# Service Objects\n\n```ruby\ndef call(**opts); { ok: true }; end\n```\n"
    )
    assert find_template_leaks(root) == []


# ---------------------------------------------------------------------------
# Frontmatter validator tests
# ---------------------------------------------------------------------------


_VALID_SKILL = """\
---
name: p
description: A test plugin
allowed-tools:
  - Read
  - Grep
---

# Plugin Body
"""

_VALID_COMMAND = """\
---
description: A test command
argument-hint: "[target]"
---

# Command Body
"""

_VALID_AGENT = """\
---
name: p-reviewer
description: Review code against the plugin's rules
model: inherit
---

# Agent Body
"""


def _write_valid_plugin(tmp_path: Path) -> Path:
    root = _mkplugin(tmp_path)
    (root / "skills/p/SKILL.md").write_text(_VALID_SKILL)
    (root / "commands/spec-test.md").write_text(_VALID_COMMAND)
    (root / "agents/reviewer.md").write_text(_VALID_AGENT)
    return root


def test_validate_frontmatter_clean_tree_has_no_issues(tmp_path: Path) -> None:
    root = _write_valid_plugin(tmp_path)
    assert validate_frontmatter(root) == []


def test_validate_frontmatter_flags_missing_block(tmp_path: Path) -> None:
    root = _write_valid_plugin(tmp_path)
    (root / "commands/spec-test.md").write_text("# Command without frontmatter\n")
    issues = validate_frontmatter(root)
    assert len(issues) == 1
    assert issues[0].kind == "missing"
    assert issues[0].category == "command"


def test_validate_frontmatter_flags_malformed_yaml(tmp_path: Path) -> None:
    root = _write_valid_plugin(tmp_path)
    (root / "agents/reviewer.md").write_text(
        "---\nname: [unterminated list\n---\n\n# Body\n"
    )
    issues = validate_frontmatter(root)
    assert len(issues) == 1
    assert issues[0].kind == "unparseable"


def test_validate_frontmatter_flags_missing_required_field(tmp_path: Path) -> None:
    root = _write_valid_plugin(tmp_path)
    (root / "skills/p/SKILL.md").write_text(
        "---\ndescription: No name here\n---\n\n# Body\n"
    )
    issues = validate_frontmatter(root)
    assert len(issues) == 1
    assert issues[0].kind == "field-missing"
    assert "name" in issues[0].message


def test_validate_frontmatter_flags_empty_description(tmp_path: Path) -> None:
    root = _write_valid_plugin(tmp_path)
    (root / "commands/spec-test.md").write_text(
        "---\ndescription: \n---\n\n# Body\n"
    )
    issues = validate_frontmatter(root)
    assert len(issues) == 1
    assert issues[0].kind == "field-wrong-type"


def test_validate_frontmatter_ignores_reference_files(tmp_path: Path) -> None:
    """Reference files under references/ are plain markdown — no frontmatter required."""
    root = _write_valid_plugin(tmp_path)
    # Reference files already exist from _mkplugin; confirm they're not flagged
    assert validate_frontmatter(root) == []
