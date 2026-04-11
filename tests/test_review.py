"""Tests for the plan review module and CLI command."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from franklin.checkpoint import RunDirectory
from franklin.cli import app
from franklin.review import apply_omissions, parse_omit_selection
from franklin.schema import (
    Artifact,
    ArtifactType,
    PlanManifest,
    PluginMeta,
)

runner = CliRunner()


def _plan(n: int = 4) -> PlanManifest:
    artifacts = [
        Artifact(
            id=f"ref.{i}",
            type=ArtifactType.REFERENCE,
            path=f"references/ref{i}.md",
            brief=f"reference {i}",
            feeds_from=[f"ch{i:02d}.concepts"],
            estimated_output_tokens=1000,
        )
        for i in range(1, n + 1)
    ]
    return PlanManifest(
        book_id="test-book",
        generated_at=datetime.now(UTC),
        planner_model="test-model",
        planner_rationale="fixture",
        plugin=PluginMeta(name="test-plugin", description="x"),
        artifacts=artifacts,
        estimated_total_output_tokens=n * 1000,
        estimated_reduce_calls=n,
    )


# ---------------------------------------------------------------------------
# parse_omit_selection
# ---------------------------------------------------------------------------


def test_parse_blank_returns_empty() -> None:
    assert parse_omit_selection("", total=5) == []
    assert parse_omit_selection("   ", total=5) == []


def test_parse_single_numbers() -> None:
    assert parse_omit_selection("1,3,5", total=5) == [1, 3, 5]


def test_parse_space_separated() -> None:
    assert parse_omit_selection("1 3 5", total=5) == [1, 3, 5]


def test_parse_range() -> None:
    assert parse_omit_selection("2-4", total=5) == [2, 3, 4]


def test_parse_mixed() -> None:
    assert parse_omit_selection("1, 3-4", total=5) == [1, 3, 4]


def test_parse_deduplicates() -> None:
    assert parse_omit_selection("1,1,2", total=5) == [1, 2]


def test_parse_rejects_out_of_bounds() -> None:
    with pytest.raises(ValueError, match="out of range"):
        parse_omit_selection("10", total=5)


def test_parse_rejects_nonnumeric() -> None:
    with pytest.raises(ValueError, match="not a number"):
        parse_omit_selection("foo", total=5)


def test_parse_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="start > end"):
        parse_omit_selection("5-2", total=5)


# ---------------------------------------------------------------------------
# apply_omissions
# ---------------------------------------------------------------------------


def test_apply_omissions_removes_matching_ids() -> None:
    plan = _plan(4)
    result = apply_omissions(plan, ["ref.2", "ref.4"])
    assert result.kept_count == 2
    assert [a.id for a in result.plan.artifacts] == ["ref.1", "ref.3"]
    assert result.omitted_ids == ["ref.2", "ref.4"]


def test_apply_omissions_updates_estimates() -> None:
    plan = _plan(4)
    result = apply_omissions(plan, ["ref.1", "ref.2"])
    assert result.plan.estimated_reduce_calls == 2
    assert result.plan.estimated_total_output_tokens == 2000


def test_apply_omissions_ignores_unknown_ids() -> None:
    plan = _plan(3)
    result = apply_omissions(plan, ["ref.1", "ref.99"])
    assert result.kept_count == 2
    assert result.omitted_ids == ["ref.1"]


def test_apply_omissions_empty_list_keeps_everything() -> None:
    plan = _plan(3)
    result = apply_omissions(plan, [])
    assert result.kept_count == 3
    assert result.omitted == []


# ---------------------------------------------------------------------------
# CLI franklin review
# ---------------------------------------------------------------------------


def _setup_run(tmp_path: Path, plan: PlanManifest) -> Path:
    run = RunDirectory(tmp_path)
    run.ensure()
    run.save_plan(plan)
    return tmp_path


def test_review_command_missing_plan(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = runner.invoke(app, ["review", str(tmp_path / "empty")])
    assert result.exit_code == 1
    assert "no plan.json" in result.output


def test_review_command_empty_plan_noop(tmp_path: Path) -> None:
    plan = _plan(0)
    _setup_run(tmp_path, plan)
    result = runner.invoke(app, ["review", str(tmp_path)])
    assert result.exit_code == 0
    assert "no artifacts" in result.output


def test_review_command_keep_all(tmp_path: Path) -> None:
    plan = _plan(3)
    _setup_run(tmp_path, plan)
    # Input: empty line (keep all)
    result = runner.invoke(app, ["review", str(tmp_path)], input="\n")
    assert result.exit_code == 0
    assert "keeping all artifacts" in result.output
    # Plan unchanged
    reloaded = RunDirectory(tmp_path).load_plan()
    assert len(reloaded.artifacts) == 3


def test_review_command_omit_and_confirm(tmp_path: Path) -> None:
    plan = _plan(4)
    _setup_run(tmp_path, plan)
    # Input: omit 1,3 then confirm
    result = runner.invoke(app, ["review", str(tmp_path)], input="1,3\ny\n")
    assert result.exit_code == 0
    assert "plan.json updated" in result.output
    reloaded = RunDirectory(tmp_path).load_plan()
    assert [a.id for a in reloaded.artifacts] == ["ref.2", "ref.4"]


def test_review_command_omit_then_decline_save(tmp_path: Path) -> None:
    plan = _plan(3)
    _setup_run(tmp_path, plan)
    result = runner.invoke(app, ["review", str(tmp_path)], input="1\nn\n")
    assert result.exit_code == 0
    assert "no changes written" in result.output
    reloaded = RunDirectory(tmp_path).load_plan()
    assert len(reloaded.artifacts) == 3


def test_review_command_rejects_invalid_selection_then_accepts(
    tmp_path: Path,
) -> None:
    plan = _plan(3)
    _setup_run(tmp_path, plan)
    # First try "foo" (invalid), then ""
    result = runner.invoke(app, ["review", str(tmp_path)], input="foo\n\n")
    assert result.exit_code == 0
    assert "not a number" in result.output
