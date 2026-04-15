"""Unit tests for ReduceService — service is Typer/Rich-free."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from _fakes import FakeAsyncClient
from franklin.checkpoint import RunDirectory
from franklin.schema import (
    Artifact,
    ArtifactType,
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterSidecar,
    Concept,
    Importance,
    PlanManifest,
    PluginMeta,
)
from franklin.services.events import ItemDone, StageFinish, StageStart
from franklin.services.reduce import (
    ArtifactNotFoundError,
    NoPlanError,
    NoSidecarsForReduceError,
    ReduceContext,
    ReduceInput,
    ReduceResult,
    ReduceService,
    UnknownArtifactTypeError,
)

_REDUCE_USAGE = {
    "input_tokens": 500,
    "output_tokens": 200,
    "cache_read_input_tokens": 300,
    "cache_creation_input_tokens": 0,
}


def _reduce_fake(body: str) -> FakeAsyncClient:
    return FakeAsyncClient({"content": body}, usage=_REDUCE_USAGE)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _book(tmp_path: Path) -> BookManifest:
    return BookManifest(
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


def _plan(n_artifacts: int = 1) -> PlanManifest:
    artifacts = [
        Artifact(
            id=f"art.skill.{i}",
            type=ArtifactType.SKILL,
            path=f"skills/s{i}/SKILL.md",
            brief=f"skill {i}",
            feeds_from=["book.metadata"],
            estimated_output_tokens=500,
        )
        for i in range(n_artifacts)
    ]
    return PlanManifest(
        book_id="test",
        generated_at=datetime.now(UTC),
        planner_model="claude-opus-4-6",
        planner_rationale="r",
        plugin=PluginMeta(name="test-plugin", version="0.1.0", description="d"),
        artifacts=artifacts,
    )


def _seed_run(tmp_path: Path, n_artifacts: int = 1) -> tuple[RunDirectory, PlanManifest]:
    run = RunDirectory(tmp_path / "run")
    run.ensure()
    run.save_book(_book(tmp_path))
    run.save_sidecar(
        ChapterSidecar(
            chapter_id="ch01",
            title="One",
            order=1,
            source_ref="pp.1",
            word_count=100,
            summary="s",
            concepts=[
                Concept(
                    id="ch01.concept.a",
                    name="A",
                    definition="a",
                    importance=Importance.HIGH,
                    source_location="§1",
                )
            ],
        )
    )
    plan = _plan(n_artifacts=n_artifacts)
    run.save_plan(plan)
    return run, plan


def _valid_md_body(name: str) -> str:
    return f'---\nname: {name}\ndescription: "test skill"\n---\n\n# Title\n\nBody.\n'


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_raises_when_no_plan(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path / "empty")
    run.ensure()
    with pytest.raises(NoPlanError):
        ReduceService().prepare(ReduceInput(run_dir=run.root))


def test_prepare_raises_when_no_sidecars(tmp_path: Path) -> None:
    run = RunDirectory(tmp_path / "run")
    run.ensure()
    run.save_book(_book(tmp_path))
    run.save_plan(_plan())
    with pytest.raises(NoSidecarsForReduceError):
        ReduceService().prepare(ReduceInput(run_dir=run.root))


# ---------------------------------------------------------------------------
# select_artifacts
# ---------------------------------------------------------------------------


def test_select_artifacts_by_id() -> None:
    plan = _plan(n_artifacts=2)
    picks = ReduceService().select_artifacts(plan, artifact_id="art.skill.1")
    assert len(picks) == 1
    assert picks[0].id == "art.skill.1"


def test_select_artifacts_unknown_id_raises() -> None:
    plan = _plan()
    with pytest.raises(ArtifactNotFoundError):
        ReduceService().select_artifacts(plan, artifact_id="missing")


def test_select_artifacts_unknown_type_raises() -> None:
    plan = _plan()
    with pytest.raises(UnknownArtifactTypeError):
        ReduceService().select_artifacts(plan, type_filter="not-a-type")


def test_select_artifacts_all_by_default() -> None:
    plan = _plan(n_artifacts=3)
    picks = ReduceService().select_artifacts(plan)
    assert len(picks) == 3


# ---------------------------------------------------------------------------
# generate / run
# ---------------------------------------------------------------------------


def test_reduce_service_generates_artifacts_and_emits_events(tmp_path: Path) -> None:
    run, plan = _seed_run(tmp_path, n_artifacts=2)
    events: list[Any] = []

    result = ReduceService().run(
        ReduceInput(run_dir=run.root, concurrency=1),
        progress=events.append,
        client=_reduce_fake(_valid_md_body("s")),
    )

    assert isinstance(result, ReduceResult)
    assert result.generated_count == 2
    assert result.skipped_count == 0
    assert result.failed_count == 0
    assert result.plugin_root == run.output_dir / plan.plugin.name

    # Files on disk
    for i in range(2):
        assert (result.plugin_root / f"skills/s{i}/SKILL.md").exists()

    # Event stream shape
    assert any(isinstance(e, StageStart) and e.total == 2 for e in events)
    ok = [e for e in events if isinstance(e, ItemDone) and e.status == "ok"]
    assert {e.item_id for e in ok} == {"art.skill.0", "art.skill.1"}
    assert any(isinstance(e, StageFinish) for e in events)


def test_reduce_service_skips_existing_unless_forced(tmp_path: Path) -> None:
    run, _ = _seed_run(tmp_path, n_artifacts=1)
    client = _reduce_fake(_valid_md_body("s"))

    first = ReduceService().run(ReduceInput(run_dir=run.root), client=client)
    assert first.generated_count == 1

    second = ReduceService().run(ReduceInput(run_dir=run.root), client=client)
    assert second.generated_count == 0
    assert second.skipped_count == 1

    forced = ReduceService().run(ReduceInput(run_dir=run.root, force=True), client=client)
    assert forced.generated_count == 1
    assert forced.skipped_count == 0


def test_generate_accepts_pre_built_context(tmp_path: Path) -> None:
    """The ``fix`` command path — caller has context + custom target list."""
    run, plan = _seed_run(tmp_path, n_artifacts=2)
    book = run.load_book()
    sidecars = {"ch01": run.load_sidecar("ch01")}
    context = ReduceContext(run=run, plan=plan, book=book, sidecars=sidecars)
    custom_targets = [plan.artifacts[0]]  # regenerate just the first

    result = ReduceService().generate(
        context,
        custom_targets,
        force=True,
        concurrency=1,
        client=_reduce_fake(_valid_md_body("s")),
    )

    assert result.generated_count == 1
    assert (result.plugin_root / plan.artifacts[0].path).exists()
    assert not (result.plugin_root / plan.artifacts[1].path).exists()


def test_reduce_service_collects_failures_nonfatally(tmp_path: Path) -> None:
    """One failing artifact doesn't stop the batch."""
    run, _plan = _seed_run(tmp_path, n_artifacts=2)

    # First call raises; subsequent calls return a valid body. Built by
    # wrapping FakeAsyncClient's stream to inject the error on call 1.
    class _BrokenClient(FakeAsyncClient):
        def __init__(self) -> None:
            super().__init__({"content": _valid_md_body("s")})
            self._call_count = 0

        def stream(self, **kwargs: Any) -> Any:
            self._call_count += 1
            if self._call_count == 1:
                raise RuntimeError("boom")
            return super().stream(**kwargs)

    result = ReduceService().run(
        ReduceInput(run_dir=run.root, concurrency=1),
        client=_BrokenClient(),
    )

    assert result.failed_count == 1
    assert result.generated_count == 1
