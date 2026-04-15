"""Smoke tests for commands added in v0.3+: diff, validate, stats, costs, fix."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from franklin.checkpoint import RunDirectory
from franklin.commands.diagnostics import costs_command, stats_command
from franklin.commands.operations import diff_command, validate_command
from franklin.schema import (
    Artifact,
    ArtifactType,
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterKind,
    NormalizedChapter,
    PlanManifest,
    PluginMeta,
    TocEntry,
)


def _seed_complete_run(root: Path, plugin_name: str = "test-plugin") -> RunDirectory:
    """Create a minimal but complete run for testing."""
    run = RunDirectory(root)
    run.ensure()

    toc = [
        TocEntry(
            id="ch01",
            title="Intro",
            source_ref="p.1",
            kind=ChapterKind.CONTENT,
            word_count=1000,
        )
    ]
    manifest = BookManifest(
        franklin_version="0.3.0",
        source=BookSource(
            path=str(root / "book.epub"),
            sha256="0" * 64,
            format="epub",
            ingested_at=datetime.now(UTC),
        ),
        metadata=BookMetadata(title="Test Book", authors=["Ada"]),
        structure=BookStructure(toc=toc),
    )
    run.save_book(manifest)
    run.save_raw_chapter(
        NormalizedChapter(
            chapter_id="ch01",
            title="Intro",
            order=1,
            source_ref="p.1",
            word_count=1000,
            text="content",
        )
    )

    plan = PlanManifest(
        book_id="test",
        planner_model="test",
        generated_at=datetime.now(UTC),
        plugin=PluginMeta(name=plugin_name, version="0.1.0", description="Test"),
        artifacts=[
            Artifact(
                id="art.ref.intro",
                type=ArtifactType.REFERENCE,
                path=f"skills/{plugin_name}/references/intro.md",
                brief="Introduction reference",
                feeds_from=["ch01"],
            ),
        ],
        coherence_rules=[],
        planner_rationale="test",
    )
    run.save_plan(plan)

    # Write a minimal reference file
    ref_path = run.output_dir / plugin_name / f"skills/{plugin_name}/references/intro.md"
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(
        "# Introduction\n\n"
        "## Problem Framing\n\n"
        "When your code is hard to change...\n\n"
        "## When to Use\n\n"
        "Use when building new features.\n\n"
        "```ruby\nclass X; end\n```\n\n"
        "See [other](intro.md) reference.\n"
    )

    # Write plugin.json
    plugin_dir = run.output_dir / plugin_name / ".claude-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        f'{{"name":"{plugin_name}","version":"0.1.0","description":"Test"}}'
    )

    return run


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_shows_output_for_completed_run(tmp_path: Path) -> None:
    _seed_complete_run(tmp_path / "runs" / "test-book")
    stats_command(base=tmp_path / "runs")


def test_stats_handles_empty_runs_dir(tmp_path: Path) -> None:
    stats_command(base=tmp_path / "runs")


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------


def test_costs_handles_no_cost_data(tmp_path: Path) -> None:
    _seed_complete_run(tmp_path / "runs" / "test-book")
    costs_command(base=tmp_path / "runs", output_json=False)


def test_costs_shows_data_when_present(tmp_path: Path) -> None:
    run = _seed_complete_run(tmp_path / "runs" / "test-book")
    run.append_cost(
        stage="map",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.05,
    )
    costs_command(base=tmp_path / "runs", output_json=False)


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


def test_validate_passes_on_good_reference(tmp_path: Path) -> None:
    run = _seed_complete_run(tmp_path / "run")
    validate_command(run_dir=run.root)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_diff_compares_two_runs(tmp_path: Path) -> None:
    _seed_complete_run(tmp_path / "run-a", plugin_name="test-a")
    _seed_complete_run(tmp_path / "run-b", plugin_name="test-b")
    diff_command(run_a=tmp_path / "run-a", run_b=tmp_path / "run-b")
