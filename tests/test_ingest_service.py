"""Unit tests for IngestService — service is fully callable without Typer/Rich."""

from __future__ import annotations

from pathlib import Path

import pytest

from franklin.schema import BookManifest
from franklin.services.events import (
    ProgressEvent,
    StageFinish,
    StageStart,
)
from franklin.services.ingest import (
    IngestInput,
    IngestResult,
    IngestService,
)

FIXTURE_EPUB = Path(__file__).resolve().parents[1] / (
    "Layered Design for Ruby on Rails Applications by Vladimir Dementyev.epub"
)


@pytest.mark.skipif(not FIXTURE_EPUB.exists(), reason="fixture EPUB not present")
def test_ingest_service_runs_without_typer(tmp_path: Path) -> None:
    events: list[ProgressEvent] = []
    result = IngestService().run(
        IngestInput(book_path=FIXTURE_EPUB, run_dir=tmp_path / "run"),
        progress=events.append,
    )

    assert isinstance(result, IngestResult)
    assert result.cleaned is False
    assert result.is_pdf is False
    assert len(result.chapters) > 0

    # Saved to disk
    assert (result.run_dir / "book.json").exists()
    assert any((result.run_dir / "raw").glob("*.json"))

    # Emitted stage boundaries
    assert any(isinstance(e, StageStart) and e.stage == "ingest" for e in events)
    assert any(isinstance(e, StageFinish) and e.stage == "ingest" for e in events)


@pytest.mark.skipif(not FIXTURE_EPUB.exists(), reason="fixture EPUB not present")
def test_ingest_service_invokes_metadata_confirm_hook(tmp_path: Path) -> None:
    """The hook runs after parsing and before save; its return value wins."""
    seen: list[BookManifest] = []

    def confirm(manifest: BookManifest) -> BookManifest:
        seen.append(manifest)
        manifest.metadata.title = "Hook-Edited Title"
        return manifest

    result = IngestService().run(
        IngestInput(book_path=FIXTURE_EPUB, run_dir=tmp_path / "run"),
        metadata_confirm=confirm,
    )

    assert len(seen) == 1
    assert result.manifest.metadata.title == "Hook-Edited Title"


@pytest.mark.skipif(not FIXTURE_EPUB.exists(), reason="fixture EPUB not present")
def test_ingest_service_clean_on_epub_is_noop(tmp_path: Path) -> None:
    """`--clean` on an EPUB emits an info event and skips cleanup entirely."""
    events: list[ProgressEvent] = []
    result = IngestService().run(
        IngestInput(book_path=FIXTURE_EPUB, run_dir=tmp_path / "run", clean=True),
        progress=events.append,
    )

    assert result.cleaned is False
    assert result.cleanup is None
    # No cleanup stage events were emitted.
    assert not any(getattr(e, "stage", None) == "cleanup" for e in events)
