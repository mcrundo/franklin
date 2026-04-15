"""Unit tests for MapService — runs without Typer/Rich; scripted fake async LLM."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from _fakes import FakeAsyncClient
from franklin.checkpoint import RunDirectory
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterKind,
    NormalizedChapter,
    TocEntry,
)
from franklin.services.events import ItemDone, StageFinish, StageStart
from franklin.services.map import (
    ChapterNotFoundError,
    MapInput,
    MapResult,
    MapService,
    RunNotIngestedError,
)

_MAP_USAGE = {"input_tokens": 100, "output_tokens": 40}


# ---------------------------------------------------------------------------
# Fixture: a seeded run directory with two raw chapters
# ---------------------------------------------------------------------------


def _seed_run(tmp_path: Path, n_chapters: int = 2) -> RunDirectory:
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
        for i in range(1, n_chapters + 1)
    ]
    manifest = BookManifest(
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
    run.save_book(manifest)
    for i in range(1, n_chapters + 1):
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


def _extraction_payload() -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# select_targets
# ---------------------------------------------------------------------------


def test_select_targets_raises_when_not_ingested(tmp_path: Path) -> None:
    with pytest.raises(RunNotIngestedError):
        MapService().select_targets(MapInput(run_dir=tmp_path / "empty"))


def test_select_targets_raises_on_unknown_chapter_id(tmp_path: Path) -> None:
    run = _seed_run(tmp_path)
    with pytest.raises(ChapterNotFoundError):
        MapService().select_targets(MapInput(run_dir=run.root, chapter_id="ch99"))


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_map_service_extracts_and_emits_events(tmp_path: Path) -> None:
    run = _seed_run(tmp_path, n_chapters=2)
    client = FakeAsyncClient(_extraction_payload(), usage=_MAP_USAGE)

    events: list[Any] = []
    result = MapService().run(
        MapInput(run_dir=run.root, concurrency=1),
        progress=events.append,
        client=client,
    )

    assert isinstance(result, MapResult)
    assert result.extracted_count == 2
    assert result.skipped_count == 0
    assert result.input_tokens == 200  # 100 per chapter, 2 chapters
    assert result.output_tokens == 80

    # Sidecars on disk
    for cid in ("ch01", "ch02"):
        assert run.sidecar_path(cid).exists()

    # Event stream shape
    assert any(isinstance(e, StageStart) and e.total == 2 for e in events)
    ok_items = [e for e in events if isinstance(e, ItemDone) and e.status == "ok"]
    assert {e.item_id for e in ok_items} == {"ch01", "ch02"}
    assert any(isinstance(e, StageFinish) for e in events)


def test_map_service_skips_existing_sidecars_unless_forced(tmp_path: Path) -> None:
    run = _seed_run(tmp_path, n_chapters=2)
    client = FakeAsyncClient(_extraction_payload(), usage=_MAP_USAGE)

    # First pass extracts both.
    MapService().run(MapInput(run_dir=run.root), client=client)

    # Second pass without force: both skipped, no LLM calls needed, no cost.
    result = MapService().run(MapInput(run_dir=run.root), client=client)
    assert result.extracted_count == 0
    assert result.skipped_count == 2
    assert result.cost_usd == 0.0


def test_map_service_honors_force(tmp_path: Path) -> None:
    run = _seed_run(tmp_path, n_chapters=2)
    client = FakeAsyncClient(_extraction_payload(), usage=_MAP_USAGE)
    MapService().run(MapInput(run_dir=run.root), client=client)

    result = MapService().run(MapInput(run_dir=run.root, force=True), client=client)
    assert result.extracted_count == 2
    assert result.skipped_count == 0
