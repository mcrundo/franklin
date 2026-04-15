"""Unit tests for AssembleService — pure Python, no LLM."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from franklin.services.assemble import (
    AssembleInput,
    AssembleService,
    PluginNotBuiltError,
)
from franklin.services.events import StageFinish, StageStart
from franklin.services.reduce import NoPlanError


def _seed_run_with_plugin(tmp_path: Path) -> tuple[RunDirectory, Path]:
    run = RunDirectory(tmp_path / "run")
    run.ensure()
    run.save_book(
        BookManifest(
            franklin_version="0.1.0",
            source=BookSource(
                path="x.epub",
                sha256="0" * 64,
                format="epub",
                ingested_at=datetime.now(UTC),
            ),
            metadata=BookMetadata(title="Test", authors=["Ada"]),
            structure=BookStructure(),
        )
    )
    plan = PlanManifest(
        book_id="test",
        generated_at=datetime.now(UTC),
        planner_model="claude-opus-4-6",
        planner_rationale="r",
        plugin=PluginMeta(name="test-plugin", version="0.1.0", description="d"),
        artifacts=[
            Artifact(
                id="art.skill.root",
                type=ArtifactType.SKILL,
                path="skills/test-plugin/SKILL.md",
                brief="root",
                feeds_from=["book.metadata"],
                estimated_output_tokens=500,
            )
        ],
    )
    run.save_plan(plan)

    plugin_root = run.output_dir / plan.plugin.name
    skill_path = plugin_root / "skills" / "test-plugin" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        '---\nname: test-plugin\ndescription: "root skill"\n---\n\n# Test\n\nBody.\n'
    )
    return run, plugin_root


def test_assemble_raises_when_no_plan(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path / "empty")
    run.ensure()
    with pytest.raises(NoPlanError):
        AssembleService().run(AssembleInput(run_dir=run.root))


def test_assemble_raises_when_plugin_not_built(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path / "run")
    run.ensure()
    run.save_book(
        BookManifest(
            franklin_version="0.1.0",
            source=BookSource(
                path="x.epub",
                sha256="0" * 64,
                format="epub",
                ingested_at=datetime.now(UTC),
            ),
            metadata=BookMetadata(title="T", authors=[]),
            structure=BookStructure(),
        )
    )
    run.save_plan(
        PlanManifest(
            book_id="test",
            generated_at=datetime.now(UTC),
            planner_model="claude-opus-4-6",
            planner_rationale="r",
            plugin=PluginMeta(name="p", version="0.1.0", description="d"),
        )
    )
    with pytest.raises(PluginNotBuiltError) as exc_info:
        AssembleService().run(AssembleInput(run_dir=run.root))
    assert exc_info.value.plugin_root.name == "p"


def test_assemble_writes_manifest_readme_gitignore(tmp_path: Path) -> None:
    run, plugin_root = _seed_run_with_plugin(tmp_path)
    result = AssembleService().run(AssembleInput(run_dir=run.root))

    assert result.manifest_path == plugin_root / ".claude-plugin" / "plugin.json"
    assert result.manifest_path.exists()
    assert result.readme_path.exists()
    assert result.gitignore_written is True
    assert (plugin_root / ".gitignore").exists()


def test_assemble_skips_gitignore_when_present(tmp_path: Path) -> None:
    run, plugin_root = _seed_run_with_plugin(tmp_path)
    (plugin_root / ".gitignore").write_text("custom\n")

    result = AssembleService().run(AssembleInput(run_dir=run.root))
    assert result.gitignore_written is False
    # Existing content preserved
    assert (plugin_root / ".gitignore").read_text() == "custom\n"


def test_assemble_reports_validator_issues_and_grade(tmp_path: Path) -> None:
    run, _ = _seed_run_with_plugin(tmp_path)
    events: list = []
    result = AssembleService().run(AssembleInput(run_dir=run.root), progress=events.append)

    # Clean golden fixture should have no issues.
    assert result.issue_count == 0
    assert result.broken_links == []
    assert result.template_leaks == []
    assert result.frontmatter_issues == []
    assert result.grade is not None
    assert result.metrics_path.exists()

    # Stage boundaries emitted
    assert any(isinstance(e, StageStart) and e.stage == "assemble" for e in events)
    assert any(isinstance(e, StageFinish) and e.stage == "assemble" for e in events)


def test_assemble_with_zip_packages_archive(tmp_path: Path) -> None:
    run, _ = _seed_run_with_plugin(tmp_path)
    result = AssembleService().run(AssembleInput(run_dir=run.root, zip_archive=True))
    assert result.archive_path is not None
    assert result.archive_path.exists()
    assert result.archive_path.suffix == ".zip"
