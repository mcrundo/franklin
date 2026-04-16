"""Tests for the async service variants (RUB-109).

These call ``run_async`` / ``generate_async`` from within
``asyncio.run`` to prove they work without nesting event loops —
the scenario a FastAPI handler would hit.
"""

from __future__ import annotations

import asyncio
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
    ChapterKind,
    ChapterSidecar,
    Concept,
    Importance,
    NormalizedChapter,
    PlanManifest,
    PluginMeta,
    TocEntry,
)
from franklin.services.ingest import IngestInput, IngestService
from franklin.services.map import MapInput, MapService
from franklin.services.reduce import ReduceInput, ReduceService

FIXTURE_EPUB = Path(__file__).resolve().parents[1] / (
    "Layered Design for Ruby on Rails Applications by Vladimir Dementyev.epub"
)


# ---------------------------------------------------------------------------
# MapService.run_async
# ---------------------------------------------------------------------------


def _seed_map_run(tmp_path: Path) -> RunDirectory:
    run = RunDirectory(tmp_path / "run")
    run.ensure()
    toc = [
        TocEntry(
            id=f"ch{i:02d}",
            title=f"Chapter {i}",
            source_ref=f"pp.{i}",
            kind=ChapterKind.CONTENT,
            word_count=1000,
        )
        for i in range(1, 3)
    ]
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
            structure=BookStructure(toc=toc),
        )
    )
    for i in range(1, 3):
        run.save_raw_chapter(
            NormalizedChapter(
                chapter_id=f"ch{i:02d}",
                title=f"Chapter {i}",
                order=i,
                source_ref=f"pp.{i}",
                word_count=1000,
                text=f"body of ch{i:02d}",
            )
        )
    return run


def _map_payload() -> dict[str, Any]:
    return {
        "summary": "Summary.",
        "concepts": [
            {
                "id": "cx.concept.a",
                "name": "A",
                "definition": "a definition",
                "importance": "high",
                "source_location": "§1",
            }
        ],
    }


def test_map_run_async_works_inside_asyncio_run(tmp_path: Path) -> None:
    """Proves run_async doesn't nest asyncio.run — the key FastAPI requirement."""
    run = _seed_map_run(tmp_path)
    client = FakeAsyncClient(_map_payload(), usage={"input_tokens": 50, "output_tokens": 20})

    async def go() -> None:
        result = await MapService().run_async(
            MapInput(run_dir=run.root, concurrency=1),
            client=client,
        )
        assert result.extracted_count == 2
        for cid in ("ch01", "ch02"):
            assert run.sidecar_path(cid).exists()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# ReduceService.generate_async
# ---------------------------------------------------------------------------


def _seed_reduce_run(tmp_path: Path) -> tuple[RunDirectory, PlanManifest]:
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
    plan = PlanManifest(
        book_id="test",
        generated_at=datetime.now(UTC),
        planner_model="claude-opus-4-6",
        planner_rationale="r",
        plugin=PluginMeta(name="test-plugin", version="0.1.0", description="d"),
        artifacts=[
            Artifact(
                id="art.skill.0",
                type=ArtifactType.SKILL,
                path="skills/s0/SKILL.md",
                brief="skill 0",
                feeds_from=["book.metadata"],
                estimated_output_tokens=500,
            )
        ],
    )
    run.save_plan(plan)
    return run, plan


def test_reduce_generate_async_works_inside_asyncio_run(tmp_path: Path) -> None:
    run, plan = _seed_reduce_run(tmp_path)
    body = '---\nname: s0\ndescription: "test"\n---\n\n# Title\n\nBody.\n'
    client = FakeAsyncClient({"content": body})

    async def go() -> None:
        service = ReduceService()
        context = service.prepare(ReduceInput(run_dir=run.root))
        result = await service.generate_async(
            context,
            plan.artifacts,
            concurrency=1,
            client=client,
        )
        assert result.generated_count == 1
        assert (result.plugin_root / "skills/s0/SKILL.md").exists()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# IngestService.run_async
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FIXTURE_EPUB.exists(), reason="fixture EPUB not present")
def test_ingest_run_async_works_inside_asyncio_run(tmp_path: Path) -> None:
    async def go() -> None:
        result = await IngestService().run_async(
            IngestInput(book_path=FIXTURE_EPUB, run_dir=tmp_path / "run"),
        )
        assert len(result.chapters) > 0
        assert (result.run_dir / "book.json").exists()

    asyncio.run(go())
