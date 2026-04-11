"""Tests for the pure-Python grade card (RUB-84).

Builds hand-rolled fixture plugin trees and plan manifests, runs
``grade_run`` over them, and asserts the composite letter and
per-artifact rubric hits. No LLM, no disk state beyond ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from franklin.checkpoint import RunDirectory
from franklin.grading import (
    grade_artifact,
    grade_run,
    write_metrics,
)
from franklin.schema import (
    Artifact,
    ArtifactType,
    PlanManifest,
    PluginMeta,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _plan_with_artifacts(
    artifacts: list[Artifact], plugin_name: str = "test-plugin"
) -> PlanManifest:
    return PlanManifest(
        book_id="test-book",
        generated_at=datetime.now(UTC),
        planner_model="test-model",
        planner_rationale="fixture plan",
        plugin=PluginMeta(name=plugin_name, description="test plugin"),
        artifacts=artifacts,
    )


def _setup_run(tmp_path: Path, plan: PlanManifest) -> tuple[RunDirectory, Path]:
    run = RunDirectory(tmp_path)
    run.ensure()
    run.save_plan(plan)
    plugin_root = run.output_dir / plan.plugin.name
    plugin_root.mkdir(parents=True, exist_ok=True)
    return run, plugin_root


def _good_reference(plugin_root: Path, path: str = "references/patterns/auth.md") -> Artifact:
    full = plugin_root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        "# Authorization pattern\n"
        "\n"
        "## When to use\n"
        "\n"
        "The problem this solves: controllers get littered with `current_user.admin?` "
        "checks that drift out of sync with policy. Use this when a controller needs "
        "to authorize an action against the current user's capabilities.\n"
        "\n"
        "See also [command spec-test](../../commands/spec-test.md).\n"
        "\n"
        "```ruby\n"
        "class PostPolicy < ApplicationPolicy\n"
        "  def update?\n"
        "    user.admin? || record.author == user\n"
        "  end\n"
        "end\n"
        "```\n" + ("\nMore explanation. " * 500)
    )
    return Artifact(
        id="ref.auth",
        type=ArtifactType.REFERENCE,
        path=path,
        brief="authorization pattern",
        feeds_from=["ch03.concepts"],
    )


def _good_command(plugin_root: Path, path: str = "commands/spec-test.md") -> Artifact:
    full = plugin_root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    body = "Explanation line. " * 200
    full.write_text(
        "---\n"
        "description: Run the spec suite for a changed file\n"
        "---\n"
        "\n"
        "## Steps\n"
        "\n"
        "1. Read the file\n"
        "2. Use Grep to find related specs\n"
        "3. Run them\n"
        "\n"
        f"{body}\n"
        "\n"
        "## Verify\n"
        "\n"
        "Check that output shows all green.\n"
    )
    return Artifact(
        id="cmd.spec-test",
        type=ArtifactType.COMMAND,
        path=path,
        brief="spec test command",
        feeds_from=["ch04.principles"],
    )


def _good_agent(plugin_root: Path, path: str = "agents/reviewer.md") -> Artifact:
    full = plugin_root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        "---\n"
        "name: test-plugin:reviewer\n"
        "description: Reviews code for layered architecture violations\n"
        "---\n"
        "\n"
        "## Role\n"
        "\n"
        "You review Rails code for layered architecture.\n"
        "\n"
        "## Principles\n"
        "\n"
        "- Models are thin\n"
        "- Services hold orchestration\n"
        "\n"
        "## Procedure\n"
        "\n"
        "1. Read the file\n"
        "2. Check for violations\n"
        "\n"
        "## Output format\n"
        "\n"
        "Reference files like `references/policy.md` using backticks.\n"
    )
    return Artifact(
        id="agent.reviewer",
        type=ArtifactType.AGENT,
        path=path,
        brief="layered rails reviewer",
        feeds_from=["ch05.rules"],
    )


def _good_skill(plugin_root: Path, path: str = "skills/main/SKILL.md") -> Artifact:
    full = plugin_root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    body = "Skill body. " * 900
    full.write_text(
        "---\n"
        "name: test-plugin\n"
        "description: Router skill\n"
        "allowed-tools: Read, Grep\n"
        "---\n"
        "\n"
        "| situation | artifact |\n"
        "| --- | --- |\n"
        "| writing auth | references/patterns/auth.md |\n"
        "\n"
        f"{body}\n"
    )
    return Artifact(
        id="skill.router",
        type=ArtifactType.SKILL,
        path=path,
        brief="router skill",
        feeds_from=["book.glossary"],
    )


# ---------------------------------------------------------------------------
# grade_run — end-to-end composite grading
# ---------------------------------------------------------------------------


def test_clean_plugin_grades_a_range(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path)
    run.ensure()
    plugin_root = run.output_dir / "test-plugin"
    plugin_root.mkdir(parents=True)
    artifacts = [
        _good_reference(plugin_root),
        _good_command(plugin_root),
        _good_agent(plugin_root),
        _good_skill(plugin_root),
    ]
    run.save_plan(_plan_with_artifacts(artifacts))

    grade = grade_run(tmp_path)

    assert grade.composite_score >= 0.90
    assert grade.letter in {"A", "A-"}
    assert grade.validator_totals.total_issues == 0
    assert grade.coverage_fraction == 1.0
    assert len(grade.artifact_grades) == 4
    assert all(g.score >= 0.83 for g in grade.artifact_grades)


def test_broken_links_lower_the_grade(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path)
    run.ensure()
    plugin_root = run.output_dir / "test-plugin"
    plugin_root.mkdir(parents=True)
    ref = _good_reference(plugin_root)
    cmd = _good_command(plugin_root)
    # Introduce a broken link in the reference
    ref_path = plugin_root / ref.path
    ref_path.write_text(ref_path.read_text() + "\nSee [missing](../does/not/exist.md)\n")
    run.save_plan(_plan_with_artifacts([ref, cmd]))

    grade = grade_run(tmp_path)

    assert grade.validator_totals.broken_links >= 1
    assert any("broken markdown link" in w for w in grade.warnings)


def test_missing_frontmatter_drops_grade(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path)
    run.ensure()
    plugin_root = run.output_dir / "test-plugin"
    plugin_root.mkdir(parents=True)
    ref = _good_reference(plugin_root)
    cmd = _good_command(plugin_root)
    # Strip frontmatter from the command
    cmd_path = plugin_root / cmd.path
    full = cmd_path.read_text()
    cmd_path.write_text(full.split("---\n", 2)[-1])
    run.save_plan(_plan_with_artifacts([ref, cmd]))

    grade = grade_run(tmp_path)

    assert grade.validator_totals.frontmatter_issues >= 1
    cmd_grade = next(g for g in grade.artifact_grades if g.artifact_id == "cmd.spec-test")
    assert "has frontmatter description" in cmd_grade.failed_checks
    assert cmd_grade.score < 1.0


def test_template_leak_reported(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path)
    run.ensure()
    plugin_root = run.output_dir / "test-plugin"
    plugin_root.mkdir(parents=True)
    ref = _good_reference(plugin_root)
    (plugin_root / ref.path).write_text(
        (plugin_root / ref.path).read_text() + "\nLeak: {{chapter_title}}\n"
    )
    run.save_plan(_plan_with_artifacts([ref]))

    grade = grade_run(tmp_path)

    assert grade.validator_totals.template_leaks >= 1
    assert any("template placeholder" in w for w in grade.warnings)


def test_failed_stage_forces_f(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path)
    run.ensure()
    plugin_root = run.output_dir / "test-plugin"
    plugin_root.mkdir(parents=True)
    artifacts = [_good_reference(plugin_root)]
    run.save_plan(_plan_with_artifacts(artifacts))

    grade = grade_run(tmp_path, failed_stages=["reduce"])

    assert grade.letter in {"D", "F"}
    assert grade.failed_stages == ["reduce"]


def test_empty_feeds_from_lowers_coverage(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path)
    run.ensure()
    plugin_root = run.output_dir / "test-plugin"
    plugin_root.mkdir(parents=True)
    ref = _good_reference(plugin_root)
    cmd = _good_command(plugin_root)
    # Remove feeds_from from one artifact
    cmd_no_feeds = Artifact(
        id=cmd.id,
        type=cmd.type,
        path=cmd.path,
        brief=cmd.brief,
        feeds_from=[],
    )
    run.save_plan(_plan_with_artifacts([ref, cmd_no_feeds]))

    grade = grade_run(tmp_path)

    assert grade.coverage_fraction == 0.5
    assert any("feeds_from" in w for w in grade.warnings)


# ---------------------------------------------------------------------------
# grade_artifact — per-type rubric spot checks
# ---------------------------------------------------------------------------


def test_agent_output_format_with_markdown_link_fails_check(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    agent_path = plugin_root / "agents/broken.md"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text(
        "---\n"
        "name: broken\n"
        "description: A broken agent\n"
        "---\n"
        "## Role\n\ntext\n\n## Principles\n\ntext\n\n## Procedure\n\ntext\n\n"
        "## Output format\n\nReference [ref.md](ref.md) for details.\n"
    )
    artifact = Artifact(
        id="agent.broken",
        type=ArtifactType.AGENT,
        path="agents/broken.md",
        brief="broken",
        feeds_from=["ch01.concepts"],
    )
    grade = grade_artifact(artifact, plugin_root)
    assert "output format examples use backticks" in grade.failed_checks


def test_missing_artifact_file_scores_zero(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    artifact = Artifact(
        id="ref.missing",
        type=ArtifactType.REFERENCE,
        path="references/missing.md",
        brief="missing",
        feeds_from=["ch01.concepts"],
    )
    grade = grade_artifact(artifact, plugin_root)
    assert grade.score == 0.0
    assert grade.letter == "F"


# ---------------------------------------------------------------------------
# write_metrics — JSON round-trip
# ---------------------------------------------------------------------------


def test_write_metrics_writes_parseable_json(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path)
    run.ensure()
    plugin_root = run.output_dir / "test-plugin"
    plugin_root.mkdir(parents=True)
    artifacts = [_good_reference(plugin_root)]
    run.save_plan(_plan_with_artifacts(artifacts))

    grade = grade_run(tmp_path)
    metrics_path = write_metrics(tmp_path, grade)

    assert metrics_path.exists()
    data = json.loads(metrics_path.read_text())
    assert data["plugin_name"] == "test-plugin"
    assert data["letter"] == grade.letter
    assert data["composite_score"] == grade.composite_score
    assert isinstance(data["artifact_grades"], list)
    assert data["validator_totals"]["markdown_files"] >= 1


def test_grade_run_raises_when_plugin_tree_missing(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path)
    run.ensure()
    run.save_plan(_plan_with_artifacts([]))

    with pytest.raises(FileNotFoundError):
        grade_run(tmp_path)
