"""Tests for the ``franklin grade`` subcommand (RUB-85)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from franklin.checkpoint import RunDirectory
from franklin.cli import app
from franklin.schema import (
    Artifact,
    ArtifactType,
    PlanManifest,
    PluginMeta,
)

runner = CliRunner()


def _write_clean_run(tmp_path: Path) -> Path:
    run = RunDirectory(tmp_path)
    run.ensure()
    plugin_root = run.output_dir / "test-plugin"
    plugin_root.mkdir(parents=True)

    ref_path = plugin_root / "references/auth.md"
    ref_path.parent.mkdir(parents=True)
    ref_path.write_text(
        "# Authorization\n"
        "\n"
        "## When to use\n"
        "\n"
        "The problem this solves: scattered admin checks. Use when a "
        "controller needs to authorize.\n"
        "\n"
        "See [skill](../skills/main/SKILL.md).\n"
        "\n"
        "```ruby\nclass X; end\n```\n" + ("\nExtra prose. " * 500)
    )

    skill_path = plugin_root / "skills/main/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\n"
        "name: test-plugin\n"
        "description: Router\n"
        "allowed-tools: Read\n"
        "---\n"
        "\n"
        "| when | use |\n| --- | --- |\n| auth | references/auth.md |\n"
        "\n" + ("Body text. " * 900)
    )

    artifacts = [
        Artifact(
            id="ref.auth",
            type=ArtifactType.REFERENCE,
            path="references/auth.md",
            brief="auth",
            feeds_from=["ch01.concepts"],
        ),
        Artifact(
            id="skill.main",
            type=ArtifactType.SKILL,
            path="skills/main/SKILL.md",
            brief="router",
            feeds_from=["book.glossary"],
        ),
    ]
    plan = PlanManifest(
        book_id="test-book",
        generated_at=datetime.now(UTC),
        planner_model="test-model",
        planner_rationale="fixture",
        plugin=PluginMeta(name="test-plugin", description="test"),
        artifacts=artifacts,
    )
    run.save_plan(plan)
    return tmp_path


def test_grade_command_on_clean_run(tmp_path: Path) -> None:
    run_dir = _write_clean_run(tmp_path)
    result = runner.invoke(app, ["grade", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "Plugin:" in result.output
    assert "test-plugin" in result.output
    assert "Grade:" in result.output
    assert "Validation" in result.output
    assert "Artifacts" in result.output


def test_grade_command_json_output(tmp_path: Path) -> None:
    run_dir = _write_clean_run(tmp_path)
    result = runner.invoke(app, ["grade", str(run_dir), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["plugin_name"] == "test-plugin"
    assert "letter" in data
    assert "composite_score" in data
    assert isinstance(data["artifact_grades"], list)


def test_grade_command_missing_plan(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = runner.invoke(app, ["grade", str(tmp_path / "empty")])
    assert result.exit_code == 1
    assert "no plan.json" in result.output


def test_grade_command_missing_plugin_tree(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path)
    run.ensure()
    run.save_plan(
        PlanManifest(
            book_id="test-book",
            generated_at=datetime.now(UTC),
            planner_model="test-model",
            planner_rationale="fixture",
            plugin=PluginMeta(name="test-plugin", description="test"),
            artifacts=[],
        )
    )
    result = runner.invoke(app, ["grade", str(tmp_path)])
    assert result.exit_code == 1
    assert "no assembled plugin tree" in result.output


def test_grade_command_reports_broken_link(tmp_path: Path) -> None:
    run_dir = _write_clean_run(tmp_path)
    ref_path = run_dir / "output/test-plugin/references/auth.md"
    ref_path.write_text(ref_path.read_text() + "\nSee [nope](../missing.md)\n")

    result = runner.invoke(app, ["grade", str(run_dir)])
    assert result.exit_code == 0
    # Validation section should show a broken link count
    assert "1 broken links" in result.output or "broken links" in result.output
