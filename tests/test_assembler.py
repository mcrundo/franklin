"""Tests for the assemble stage."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from franklin.assembler import (
    check_citations,
    find_template_leaks,
    generate_readme,
    has_errors,
    lint_plugin,
    normalize_description,
    package_plugin,
    relpath_from,
    rewrite_root_relative_links,
    validate_frontmatter,
    validate_links,
    write_plugin_manifest,
)
from franklin.schema import (
    Artifact,
    ArtifactType,
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    PlanManifest,
    PluginMeta,
)


def _book(title: str = "Gap Selling") -> BookManifest:
    return BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path="x.epub", sha256="0" * 64, format="epub", ingested_at=datetime.now(UTC)
        ),
        metadata=BookMetadata(title=title, authors=["Keenan"]),
        structure=BookStructure(),
    )


def _plan_with_command(brief: str) -> PlanManifest:
    return PlanManifest(
        book_id="b",
        generated_at=datetime.now(UTC),
        planner_model="claude-opus-4-6",
        planner_rationale="r",
        plugin=PluginMeta(name="gap-selling", version="0.1.0", description="d"),
        artifacts=[
            Artifact(
                id="art.command.pic",
                type=ArtifactType.COMMAND,
                path="commands/build-pic.md",
                brief=brief,
                feeds_from=["book.metadata"],
                estimated_output_tokens=500,
            )
        ],
    )


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
        "# Query Objects\nSee [service objects](service-objects.md#when-to-use).\n"
    )
    assert validate_links(root) == []


def test_validate_links_reports_line_numbers(tmp_path: Path) -> None:
    root = _mkplugin(tmp_path)
    (root / "skills/p/references/patterns/query-objects.md").write_text(
        "# Query Objects\n\nBody text.\n\n[broken](missing.md)\n"
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
    # Use a YAML syntax that the repair heuristic can't fix: a tab character
    # in an indentation-sensitive context breaks the YAML parser outright.
    (root / "agents/reviewer.md").write_text("---\n\t: broken\n---\n\n# Body\n")
    issues = validate_frontmatter(root)
    assert len(issues) == 1
    assert issues[0].kind == "unparseable"


def test_validate_frontmatter_repairs_unquoted_colons(tmp_path: Path) -> None:
    """LLMs sometimes put Rails migration syntax into YAML descriptions,
    causing colons that break the parser. The repair heuristic should
    quote the value and parse successfully."""
    root = _write_valid_plugin(tmp_path)
    (root / "commands/spec-test.md").write_text(
        "---\ndescription: Create a migration: null: false, foreign_key: true\n---\n\n# Body\n"
    )
    issues = validate_frontmatter(root)
    assert not issues


def test_validate_frontmatter_flags_missing_required_field(tmp_path: Path) -> None:
    root = _write_valid_plugin(tmp_path)
    (root / "skills/p/SKILL.md").write_text("---\ndescription: No name here\n---\n\n# Body\n")
    issues = validate_frontmatter(root)
    assert len(issues) == 1
    assert issues[0].kind == "field-missing"
    assert "name" in issues[0].message


def test_validate_frontmatter_flags_empty_description(tmp_path: Path) -> None:
    root = _write_valid_plugin(tmp_path)
    (root / "commands/spec-test.md").write_text("---\ndescription: \n---\n\n# Body\n")
    issues = validate_frontmatter(root)
    assert len(issues) == 1
    assert issues[0].kind == "field-wrong-type"


def test_validate_frontmatter_ignores_reference_files(tmp_path: Path) -> None:
    """Reference files under references/ are plain markdown — no frontmatter required."""
    root = _write_valid_plugin(tmp_path)
    # Reference files already exist from _mkplugin; confirm they're not flagged
    assert validate_frontmatter(root) == []


# ---------------------------------------------------------------------------
# Packager tests
# ---------------------------------------------------------------------------


def test_package_plugin_writes_a_zip_with_plugin_prefixed_entries(tmp_path: Path) -> None:
    plugin_root = tmp_path / "my-plugin"
    (plugin_root / "skills/my-plugin/references").mkdir(parents=True)
    (plugin_root / "commands").mkdir(parents=True)
    (plugin_root / "skills/my-plugin/SKILL.md").write_text("# Skill")
    (plugin_root / "skills/my-plugin/references/foo.md").write_text("# Foo")
    (plugin_root / "commands/do-thing.md").write_text("# Do Thing")

    archive = tmp_path / "my-plugin.zip"
    result = package_plugin(plugin_root, archive)

    assert result == archive
    assert archive.exists()
    with zipfile.ZipFile(archive) as zf:
        names = sorted(zf.namelist())
    assert names == [
        "my-plugin/commands/do-thing.md",
        "my-plugin/skills/my-plugin/SKILL.md",
        "my-plugin/skills/my-plugin/references/foo.md",
    ]


def test_package_plugin_creates_output_parent_dir(tmp_path: Path) -> None:
    plugin_root = tmp_path / "p"
    plugin_root.mkdir()
    (plugin_root / "a.md").write_text("x")

    archive = tmp_path / "sub/nested/p.zip"
    package_plugin(plugin_root, archive)
    assert archive.exists()


def test_package_plugin_contents_match_source_files(tmp_path: Path) -> None:
    plugin_root = tmp_path / "p"
    plugin_root.mkdir()
    (plugin_root / "a.md").write_text("alpha\n")
    (plugin_root / "b.md").write_text("beta\n")

    archive = tmp_path / "p.zip"
    package_plugin(plugin_root, archive)

    with zipfile.ZipFile(archive) as zf:
        assert zf.read("p/a.md").decode() == "alpha\n"
        assert zf.read("p/b.md").decode() == "beta\n"


# ---------------------------------------------------------------------------
# README command table uses the authoritative frontmatter description (#3)
# ---------------------------------------------------------------------------


def test_readme_command_purpose_uses_frontmatter_description(tmp_path: Path) -> None:
    plugin_root = tmp_path / "gap-selling"
    cmd = plugin_root / "commands" / "build-pic.md"
    cmd.parent.mkdir(parents=True)
    cmd.write_text(
        "---\n"
        "description: Build a Problem Identification Chart mapping problems, "
        "impact, and root causes\n"
        "---\n\n# Build PIC\n"
    )
    # A deliberately long brief that the old _first_sentence() would have
    # truncated mid-word with a dangling "...".
    plan = _plan_with_command(
        brief="Build a Problem Identification Chart by listing the problems your "
        "product solves, the business impact of each problem, and the root causes "
        "behind them so discovery has a target"
    )

    generate_readme(plugin_root, plan=plan, book=_book())
    readme = (plugin_root / "README.md").read_text()

    assert (
        "| `/gap-selling:build-pic` | "
        "Build a Problem Identification Chart mapping problems, impact, and root causes |"
    ) in readme
    # No table cell may end in a dangling ellipsis.
    assert "...|" not in readme.replace(" ", "")
    assert "…|" not in readme.replace(" ", "")


def test_readme_command_purpose_falls_back_to_brief(tmp_path: Path) -> None:
    """When a command file lacks a usable description, fall back to the brief."""
    plugin_root = tmp_path / "gap-selling"
    cmd = plugin_root / "commands" / "build-pic.md"
    cmd.parent.mkdir(parents=True)
    cmd.write_text("# Build PIC\n")  # no frontmatter

    plan = _plan_with_command(brief="Build a Problem Identification Chart")
    generate_readme(plugin_root, plan=plan, book=_book())
    readme = (plugin_root / "README.md").read_text()
    assert "Build a Problem Identification Chart" in readme


def test_readme_install_uses_real_repo_when_provided(tmp_path: Path) -> None:
    plugin_root = tmp_path / "gap-selling"
    plugin_root.mkdir()
    plan = _plan_with_command(brief="x")
    generate_readme(plugin_root, plan=plan, book=_book(), repo="dchuk/gap-selling")
    readme = (plugin_root / "README.md").read_text()
    assert "claude plugin marketplace add dchuk/gap-selling" in readme
    assert "owner/repo" not in readme


# ---------------------------------------------------------------------------
# Cross-artifact link repair (#1)
# ---------------------------------------------------------------------------


def test_relpath_from_computes_file_relative_paths() -> None:
    assert (
        relpath_from("agents/deal-reviewer.md", "commands/run-discovery.md")
        == "../commands/run-discovery.md"
    )
    assert (
        relpath_from("skills/p/references/anti-patterns/x.md", "commands/y.md")
        == "../../../../commands/y.md"
    )
    assert (
        relpath_from("skills/p/SKILL.md", "skills/p/references/patterns/x.md")
        == "references/patterns/x.md"
    )


def test_rewrite_fixes_root_relative_command_link() -> None:
    known = {"agents/deal-reviewer.md", "commands/run-discovery.md"}
    content = "See [discovery](commands/run-discovery.md) first.\n"
    fixed = rewrite_root_relative_links(content, "agents/deal-reviewer.md", known)
    assert fixed == "See [discovery](../commands/run-discovery.md) first.\n"


def test_rewrite_fixes_dropped_skill_prefix_via_suffix_match() -> None:
    known = {
        "agents/deal-reviewer.md",
        "skills/gap-selling/references/anti-patterns/common-selling-mistakes.md",
    }
    content = "Ref: [x](references/anti-patterns/common-selling-mistakes.md)\n"
    fixed = rewrite_root_relative_links(content, "agents/deal-reviewer.md", known)
    assert "../skills/gap-selling/references/anti-patterns/common-selling-mistakes.md" in fixed


def test_rewrite_leaves_correct_links_untouched() -> None:
    known = {
        "skills/p/SKILL.md",
        "skills/p/references/core/x.md",
    }
    # SKILL.md correctly links file-relative; must not be rewritten.
    content = "[core](references/core/x.md)\n"
    assert rewrite_root_relative_links(content, "skills/p/SKILL.md", known) == content


def test_rewrite_ignores_external_and_placeholder_links() -> None:
    known = {"commands/run-discovery.md"}
    content = "[site](https://example.com) and [tmpl](commands/<name>.md) and [a](#x)\n"
    assert rewrite_root_relative_links(content, "agents/r.md", known) == content


def test_rewrite_preserves_fragments() -> None:
    known = {"agents/r.md", "commands/run-discovery.md"}
    content = "[d](commands/run-discovery.md#step-2)\n"
    fixed = rewrite_root_relative_links(content, "agents/r.md", known)
    assert fixed == "[d](../commands/run-discovery.md#step-2)\n"


# ---------------------------------------------------------------------------
# Frontmatter description normalization + multi-line detection (#5)
# ---------------------------------------------------------------------------


def test_normalize_folds_block_scalar_description() -> None:
    content = (
        "---\n"
        "name: gap-selling\n"
        "description: >\n"
        "  Gap Selling methodology plugin. Use when diagnosing buyer\n"
        "  problems or running discovery conversations.\n"
        "allowed-tools:\n"
        "  - Read\n"
        "---\n\n# Body\n"
    )
    out = normalize_description(content)
    assert (
        'description: "Gap Selling methodology plugin. Use when diagnosing '
        'buyer problems or running discovery conversations."' in out
    )
    assert "description: >" not in out
    # Other frontmatter and the body are preserved.
    assert "name: gap-selling" in out
    assert "allowed-tools:" in out
    assert "# Body" in out


def test_normalize_leaves_single_line_description_untouched() -> None:
    content = '---\ndescription: "Already one line"\n---\n\n# Body\n'
    assert normalize_description(content) == content


def test_normalize_folds_plain_wrapped_description() -> None:
    content = (
        "---\n"
        "description:\n"
        "  First part of the sentence\n"
        "  and the second part.\n"
        "name: x\n"
        "---\n\n# Body\n"
    )
    out = normalize_description(content)
    assert 'description: "First part of the sentence and the second part."' in out


def test_validate_flags_block_scalar_description(tmp_path: Path) -> None:
    root = tmp_path
    (root / "skills" / "p").mkdir(parents=True)
    (root / "skills" / "p" / "SKILL.md").write_text(
        "---\nname: p\ndescription: >\n  line one\n  line two\n---\n\n# Body\n"
    )
    issues = validate_frontmatter(root)
    assert any(i.kind == "description-multiline" for i in issues)


def test_validate_passes_single_line_description(tmp_path: Path) -> None:
    root = tmp_path
    (root / "skills" / "p").mkdir(parents=True)
    (root / "skills" / "p" / "SKILL.md").write_text(
        '---\nname: p\ndescription: "one line"\n---\n\n# Body\n'
    )
    issues = validate_frontmatter(root)
    assert not any(i.kind == "description-multiline" for i in issues)


# ---------------------------------------------------------------------------
# Citation integrity heuristic (#7)
# ---------------------------------------------------------------------------


def test_check_citations_flags_prose_chapter_unbacked_by_source(tmp_path: Path) -> None:
    root = tmp_path
    (root / "skills" / "p" / "references").mkdir(parents=True)
    (root / "skills" / "p" / "references" / "leadership.md").write_text(
        "# Leadership\n\n"
        "Chapter 21 closes the book by shifting focus to the leader.\n\n"
        "A coaching culture beats a selling culture. _source: ch28 §2_\n"
    )
    mismatches = check_citations(root)
    assert len(mismatches) == 1
    assert mismatches[0].prose_chapter == 21
    assert mismatches[0].cited_chapters == (28,)


def test_check_citations_allows_matching_chapter(tmp_path: Path) -> None:
    root = tmp_path
    (root / "skills" / "p" / "references").mkdir(parents=True)
    (root / "skills" / "p" / "references" / "x.md").write_text(
        "As Chapter 28 explains, leadership matters. _source: ch28 §2_\n"
    )
    assert check_citations(root) == []


def test_check_citations_skips_files_without_source_tags(tmp_path: Path) -> None:
    root = tmp_path
    (root / "skills" / "p").mkdir(parents=True)
    (root / "skills" / "p" / "SKILL.md").write_text("See Chapter 3 for the framework.\n")
    assert check_citations(root) == []


# ---------------------------------------------------------------------------
# Lint aggregator gate
# ---------------------------------------------------------------------------


def _clean_plugin(tmp_path: Path) -> Path:
    root = tmp_path / "p"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "0.1.0", "description": "d"})
    )
    (root / "skills" / "p").mkdir(parents=True)
    (root / "skills" / "p" / "SKILL.md").write_text(
        '---\nname: p\ndescription: "one line"\n---\n\n# Body\n'
    )
    return root


def test_lint_clean_plugin_has_no_findings(tmp_path: Path) -> None:
    assert lint_plugin(_clean_plugin(tmp_path)) == []


def test_lint_reports_broken_link_as_error(tmp_path: Path) -> None:
    root = _clean_plugin(tmp_path)
    (root / "skills" / "p" / "SKILL.md").write_text(
        '---\nname: p\ndescription: "one line"\n---\n\n[x](nope.md)\n'
    )
    findings = lint_plugin(root)
    assert has_errors(findings)
    assert any(f.check == "broken_link" for f in findings)


def test_lint_reports_block_scalar_description_as_error(tmp_path: Path) -> None:
    root = _clean_plugin(tmp_path)
    (root / "skills" / "p" / "SKILL.md").write_text(
        "---\nname: p\ndescription: >\n  line one\n  line two\n---\n\n# Body\n"
    )
    findings = lint_plugin(root)
    assert any(f.check == "frontmatter" and "multiline" in f.message for f in findings)


def test_lint_reports_missing_manifest_field(tmp_path: Path) -> None:
    root = _clean_plugin(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "0.1.0"})  # no description
    )
    findings = lint_plugin(root)
    assert any(f.check == "manifest_field" for f in findings)


def test_lint_citation_mismatch_is_warning_not_error(tmp_path: Path) -> None:
    root = _clean_plugin(tmp_path)
    (root / "skills" / "p" / "references").mkdir(parents=True)
    (root / "skills" / "p" / "references" / "x.md").write_text(
        "Chapter 21 closes the book. _source: ch28 §2_\n"
    )
    findings = lint_plugin(root)
    assert not has_errors(findings)
    assert any(f.check == "citation_mismatch" and f.severity == "warning" for f in findings)


def test_lint_bundle_mode_requires_author_and_no_placeholder(tmp_path: Path) -> None:
    root = _clean_plugin(tmp_path)
    (root / "README.md").write_text("## Install\n\nclaude plugin marketplace add owner/repo\n")
    findings = lint_plugin(root, bundle=True)
    assert any(f.check == "manifest_field" and "author" in f.message for f in findings)
    assert any(f.check == "default_placeholder" for f in findings)
