"""Tests for run history summaries and ``franklin runs list``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from franklin.checkpoint import RunDirectory, list_runs, summarize_run
from franklin.cli import app
from franklin.schema import (
    Artifact,
    ArtifactType,
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    NormalizedChapter,
    PlanManifest,
    PluginMeta,
    TocEntry,
)

runner = CliRunner()


def _make_book(title: str = "Test Book") -> BookManifest:
    return BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path="/books/test.epub",
            sha256="deadbeef",
            format="epub",
            ingested_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
        ),
        metadata=BookMetadata(title=title, authors=["Alice"]),
        structure=BookStructure(toc=[TocEntry(id="ch01", title="Intro", source_ref="pp 1")]),
    )


def _plan() -> PlanManifest:
    return PlanManifest(
        book_id="test",
        generated_at=datetime.now(UTC),
        planner_model="test",
        planner_rationale="x",
        plugin=PluginMeta(name="test-plugin", description="x"),
        artifacts=[
            Artifact(
                id="ref.a",
                type=ArtifactType.REFERENCE,
                path="references/a.md",
                brief="x",
                feeds_from=["ch01.concepts"],
            )
        ],
    )


def _fresh_ingest_only(tmp_path: Path, slug: str = "slug-a") -> Path:
    run_dir = tmp_path / slug
    run = RunDirectory(run_dir)
    run.ensure()
    run.save_book(_make_book(title="Ingest Only"))
    run.save_raw_chapter(
        NormalizedChapter(
            chapter_id="ch01",
            title="Ch1",
            order=1,
            source_ref="pp 1",
            word_count=3,
            text="one two three",
        )
    )
    return run_dir


def _assembled_run(tmp_path: Path, slug: str = "slug-b") -> Path:
    run_dir = tmp_path / slug
    run = RunDirectory(run_dir)
    run.ensure()
    run.save_book(_make_book(title="Assembled Book"))
    run.save_raw_chapter(
        NormalizedChapter(
            chapter_id="ch01",
            title="Ch1",
            order=1,
            source_ref="pp 1",
            word_count=3,
            text="one two three",
        )
    )
    run.save_plan(_plan())
    plugin_dir = run.output_dir / "test-plugin"
    (plugin_dir / "references").mkdir(parents=True)
    (plugin_dir / "references/a.md").write_text("# A\n")
    (plugin_dir / ".claude-plugin").mkdir()
    (plugin_dir / ".claude-plugin/plugin.json").write_text("{}")
    (run_dir / "metrics.json").write_text(json.dumps({"letter": "A-", "composite_score": 0.92}))
    return run_dir


# ---------------------------------------------------------------------------
# summarize_run
# ---------------------------------------------------------------------------


def test_summarize_run_ingest_only(tmp_path: Path) -> None:
    run_dir = _fresh_ingest_only(tmp_path)
    s = summarize_run(run_dir)
    assert s.slug == "slug-a"
    assert s.title == "Ingest Only"
    assert s.authors == ["Alice"]
    assert s.stages_done == ["ingest"]
    assert s.last_stage == "ingest"
    assert s.artifact_count is None
    assert s.grade_letter is None


def test_summarize_run_full_pipeline(tmp_path: Path) -> None:
    run_dir = _assembled_run(tmp_path)
    s = summarize_run(run_dir)
    assert s.title == "Assembled Book"
    assert "assemble" in s.stages_done
    assert s.last_stage == "assemble"
    assert s.artifact_count == 1
    assert s.grade_letter == "A-"
    assert s.grade_score == 0.92


def test_summarize_run_empty_dir(tmp_path: Path) -> None:
    run_dir = tmp_path / "empty"
    run_dir.mkdir()
    s = summarize_run(run_dir)
    assert s.slug == "empty"
    assert s.stages_done == []
    assert s.title is None


def test_summarize_run_corrupt_book_json_doesnt_raise(tmp_path: Path) -> None:
    run_dir = tmp_path / "corrupt"
    run = RunDirectory(run_dir)
    run.ensure()
    run.book_json.write_text("{ not valid json")
    s = summarize_run(run_dir)
    assert s.title is None


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


def test_list_runs_returns_empty_for_missing_base(tmp_path: Path) -> None:
    assert list_runs(tmp_path / "does-not-exist") == []


def test_list_runs_sorts_newest_first(tmp_path: Path) -> None:
    _fresh_ingest_only(tmp_path, slug="old")
    # Override the ingested_at for the assembled run to be newer
    b = _assembled_run(tmp_path, slug="new")
    run = RunDirectory(b)
    book = run.load_book()
    book_new = book.model_copy(
        update={
            "source": book.source.model_copy(
                update={"ingested_at": datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)}
            )
        }
    )
    run.save_book(book_new)

    summaries = list_runs(tmp_path)
    assert [s.slug for s in summaries] == ["new", "old"]


def test_list_runs_skips_dotfiles(tmp_path: Path) -> None:
    _fresh_ingest_only(tmp_path, slug=".hidden")
    _fresh_ingest_only(tmp_path, slug="visible")
    slugs = [s.slug for s in list_runs(tmp_path)]
    assert slugs == ["visible"]


# ---------------------------------------------------------------------------
# franklin runs list CLI
# ---------------------------------------------------------------------------


def test_cli_runs_list_empty(tmp_path: Path) -> None:
    result = runner.invoke(app, ["runs", "list", "--base", str(tmp_path)])
    assert result.exit_code == 0
    assert "no runs found" in result.output


def test_cli_runs_list_shows_table(tmp_path: Path) -> None:
    _assembled_run(tmp_path, slug="mybook")
    result = runner.invoke(app, ["runs", "list", "--base", str(tmp_path)])
    assert result.exit_code == 0
    assert "mybook" in result.output
    assert "Assembled Book" in result.output
    assert "A-" in result.output


def test_cli_runs_list_json(tmp_path: Path) -> None:
    _assembled_run(tmp_path, slug="mybook")
    result = runner.invoke(app, ["runs", "list", "--base", str(tmp_path), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["slug"] == "mybook"
    assert data[0]["grade_letter"] == "A-"
    assert data[0]["last_stage"] == "assemble"
