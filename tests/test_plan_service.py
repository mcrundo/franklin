"""Unit tests for PlanService — runs without Typer/Rich; scripted fake client."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from _fakes import FakeClient
from franklin.checkpoint import RunDirectory
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterSidecar,
    Concept,
    Importance,
    PlanManifest,
)
from franklin.services.events import StageFinish, StageStart
from franklin.services.map import RunNotIngestedError
from franklin.services.plan import (
    NoSidecarsError,
    PlanAlreadyExistsError,
    PlanInput,
    PlanResult,
    PlanService,
)

_PLAN_USAGE = {"input_tokens": 10_000, "output_tokens": 2_000}


def _seed_run_with_sidecars(tmp_path: Path) -> RunDirectory:
    run = RunDirectory(tmp_path / "run")
    run.ensure()
    run.save_book(
        BookManifest(
            franklin_version="0.1.0",
            source=BookSource(
                path=str(tmp_path / "book.epub"),
                sha256="0" * 64,
                format="epub",
                ingested_at=datetime.now(UTC),
            ),
            metadata=BookMetadata(title="Test", authors=["Ada"]),
            structure=BookStructure(),
        )
    )
    for cid in ("ch01", "ch02"):
        run.save_sidecar(
            ChapterSidecar(
                chapter_id=cid,
                title=f"Chapter {cid}",
                order=int(cid[2:]),
                source_ref=f"pp.{cid}",
                word_count=1000,
                summary=f"summary of {cid}",
                concepts=[
                    Concept(
                        id=f"{cid}.concept.a",
                        name="A",
                        definition="a definition",
                        importance=Importance.HIGH,
                        source_location=f"{cid} §1",
                    )
                ],
            )
        )
    return run


def _plan_proposal() -> dict[str, Any]:
    return {
        "plugin": {
            "name": "test-plugin",
            "version": "0.1.0",
            "description": "A test",
            "keywords": [],
        },
        "planner_rationale": "Minimal plan.",
        "artifacts": [],
        "coherence_rules": [],
        "skipped_artifact_types": [],
        "estimated_total_output_tokens": 0,
        "estimated_reduce_calls": 0,
    }


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_raises_when_not_ingested(tmp_path: Path) -> None:
    with pytest.raises(RunNotIngestedError):
        PlanService().prepare(PlanInput(run_dir=tmp_path / "empty"))


def test_prepare_raises_when_no_sidecars(tmp_path: Path) -> None:
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
    with pytest.raises(NoSidecarsError):
        PlanService().prepare(PlanInput(run_dir=run.root))


def test_prepare_raises_when_plan_exists_without_force(tmp_path: Path) -> None:
    run = _seed_run_with_sidecars(tmp_path)
    # Seed a plan.json
    run.plan_json.write_text("{}", encoding="utf-8")

    with pytest.raises(PlanAlreadyExistsError) as exc_info:
        PlanService().prepare(PlanInput(run_dir=run.root))
    assert exc_info.value.plan_path == run.plan_json


def test_prepare_allows_overwrite_when_force(tmp_path: Path) -> None:
    run = _seed_run_with_sidecars(tmp_path)
    run.plan_json.write_text("{}", encoding="utf-8")
    # No exception when force=True
    PlanService().prepare(PlanInput(run_dir=run.root, force=True))


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_plan_service_runs_and_emits_events(tmp_path: Path) -> None:
    run = _seed_run_with_sidecars(tmp_path)
    events: list[Any] = []

    result = PlanService().run(
        PlanInput(run_dir=run.root),
        progress=events.append,
        client=FakeClient(_plan_proposal(), usage=_PLAN_USAGE),
    )

    assert isinstance(result, PlanResult)
    assert isinstance(result.plan, PlanManifest)
    assert result.plan.plugin.name == "test-plugin"
    assert result.input_tokens == 10_000
    assert result.output_tokens == 2_000
    assert result.cost_usd > 0
    assert run.plan_json.exists()

    assert any(isinstance(e, StageStart) and e.stage == "plan" for e in events)
    assert any(isinstance(e, StageFinish) and e.stage == "plan" for e in events)
