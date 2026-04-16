"""Unit tests for RunsService — no Typer/Rich, pure library queries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from franklin.checkpoint import RunDirectory
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
from franklin.services.runs import CostReport, RunDetail, RunsService


def _seed_run(base: Path, slug: str, *, assemble: bool = False) -> RunDirectory:
    run = RunDirectory(base / slug)
    run.ensure()
    run.save_book(
        BookManifest(
            franklin_version="0.1.0",
            source=BookSource(
                path=f"{slug}.epub",
                sha256="0" * 64,
                format="epub",
                ingested_at=datetime.now(UTC),
            ),
            metadata=BookMetadata(title=f"Book {slug}", authors=["Ada"]),
            structure=BookStructure(),
        )
    )
    plan = PlanManifest(
        book_id=slug,
        generated_at=datetime.now(UTC),
        planner_model="claude-opus-4-6",
        planner_rationale="r",
        plugin=PluginMeta(name=slug, version="0.1.0", description="d"),
        artifacts=[
            Artifact(
                id=f"art.skill.{slug}",
                type=ArtifactType.SKILL,
                path=f"skills/{slug}/SKILL.md",
                brief="b",
                feeds_from=["book.metadata"],
            )
        ],
    )
    run.save_plan(plan)

    if assemble:
        plugin_dir = run.output_dir / slug / ".claude-plugin"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "plugin.json").write_text(f'{{"name":"{slug}"}}')
        skill_dir = run.output_dir / slug / "skills" / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f'---\nname: {slug}\ndescription: "test"\n---\n\n# {slug}\n'
        )

    run.append_cost(
        stage="map",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=200,
        cost_usd=0.05,
    )
    run.append_cost(
        stage="plan",
        model="claude-opus-4-6",
        input_tokens=5000,
        output_tokens=800,
        cost_usd=0.30,
    )

    return run


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_returns_summaries(tmp_path: Path) -> None:
    _seed_run(tmp_path, "book-a")
    _seed_run(tmp_path, "book-b")

    result = RunsService().list(tmp_path)
    slugs = {s.slug for s in result}
    assert "book-a" in slugs
    assert "book-b" in slugs


def test_list_returns_empty_for_missing_base(tmp_path: Path) -> None:
    result = RunsService().list(tmp_path / "nope")
    assert result == []


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_returns_detail_with_costs(tmp_path: Path) -> None:
    run = _seed_run(tmp_path, "book-x")
    detail = RunsService().get(run.root)

    assert isinstance(detail, RunDetail)
    assert detail.summary.slug == "book-x"
    assert detail.summary.title == "Book book-x"
    assert len(detail.costs) == 2
    assert detail.total_cost_usd == 0.35  # 0.05 + 0.30
    assert detail.grade is None  # not assembled


def test_get_returns_grade_when_assembled(tmp_path: Path) -> None:
    run = _seed_run(tmp_path, "book-graded", assemble=True)
    # Write metrics so grade_run picks it up
    from franklin.grading import grade_run, write_metrics

    grade = grade_run(run.root)
    write_metrics(run.root, grade)

    detail = RunsService().get(run.root)
    assert detail.grade is not None
    assert detail.grade.letter is not None


# ---------------------------------------------------------------------------
# costs
# ---------------------------------------------------------------------------


def test_costs_aggregates_across_runs(tmp_path: Path) -> None:
    _seed_run(tmp_path, "run-1")
    _seed_run(tmp_path, "run-2")

    report = RunsService().costs(tmp_path)

    assert isinstance(report, CostReport)
    assert len(report.runs) == 2
    assert report.grand_total_usd == 0.70  # 0.35 per run, 2 runs

    stage_names = {s.stage for s in report.by_stage}
    assert "map" in stage_names
    assert "plan" in stage_names


def test_costs_empty_base(tmp_path: Path) -> None:
    report = RunsService().costs(tmp_path / "nope")
    assert report.runs == []
    assert report.grand_total_usd == 0.0
