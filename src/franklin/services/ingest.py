"""Ingest stage service — parse a book, optionally clean it, classify, save.

Orchestrates the deterministic ingest pipeline plus the optional Tier 4
LLM cleanup pass. The Typer command in ``franklin.cli`` is a thin shell
that builds ``IngestInput``, subscribes to progress events, and renders.

Interactive metadata confirmation stays in the CLI: the service exposes
an optional ``metadata_confirm`` hook so it can run inline between
parse and save without dragging Typer prompts into the service layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from franklin.checkpoint import RunDirectory
from franklin.classify import classify_chapters
from franklin.ingest import ingest_book
from franklin.ingest.cleanup import clean_chapters_async
from franklin.schema import BookManifest, NormalizedChapter
from franklin.services.events import (
    InfoEvent,
    ItemDone,
    ProgressCallback,
    StageFinish,
    StageStart,
    WarningEvent,
)

_STAGE = "ingest"
_CLEANUP_STAGE = "cleanup"
_CLEANUP_MODEL = "claude-sonnet-4-6"


def _sonnet_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * 3.0 + (output_tokens / 1_000_000) * 15.0


MetadataConfirmHook = Callable[[BookManifest], BookManifest]
"""Callback invoked after parsing, before cleanup/save.

Returns the (possibly user-edited) manifest. The CLI uses this to run
an interactive confirm; library callers pass ``None`` and get the raw
parsed metadata through.
"""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestInput(_Base):
    book_path: Path
    run_dir: Path
    clean: bool = False
    clean_concurrency: int = Field(default=8, ge=1, le=32)


class CleanupStats(_Base):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    failed_ids: list[str] = Field(default_factory=list)


class IngestResult(_Base):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_dir: Path
    manifest: BookManifest
    chapters: list[NormalizedChapter]
    is_pdf: bool
    cleaned: bool
    cleanup: CleanupStats | None = None


class IngestService:
    """Parse → (optional clean) → classify → save. No interactive output."""

    def run(
        self,
        params: IngestInput,
        *,
        progress: ProgressCallback | None = None,
        metadata_confirm: MetadataConfirmHook | None = None,
        cleanup_client: Any | None = None,
    ) -> IngestResult:
        emit = progress or (lambda _event: None)
        is_pdf = params.book_path.suffix.lower() == ".pdf"

        run = RunDirectory(params.run_dir)
        run.ensure()

        emit(StageStart(stage=_STAGE))
        emit(InfoEvent(stage=_STAGE, message=f"Ingesting {params.book_path}"))

        manifest, chapters = ingest_book(params.book_path)

        if metadata_confirm is not None:
            manifest = metadata_confirm(manifest)

        do_clean = params.clean and is_pdf
        if params.clean and not is_pdf:
            emit(
                InfoEvent(
                    stage=_STAGE,
                    message="--clean is a no-op on EPUBs (already structurally clean)",
                )
            )

        cleanup_stats: CleanupStats | None = None
        if do_clean:
            chapters, cleanup_stats = self._run_cleanup(
                chapters,
                concurrency=params.clean_concurrency,
                client=cleanup_client,
                emit=emit,
            )
            run.append_cost(
                stage="cleanup",
                model=_CLEANUP_MODEL,
                input_tokens=cleanup_stats.input_tokens,
                output_tokens=cleanup_stats.output_tokens,
                cost_usd=cleanup_stats.cost_usd,
            )
            # Rebuild structure totals from the cleaned chapters so book.json
            # reflects the post-cleanup word counts.
            manifest.structure.total_words = sum(c.word_count for c in chapters)
            by_id = {e.id: e for e in manifest.structure.toc}
            for chapter in chapters:
                entry = by_id.get(chapter.chapter_id)
                if entry is not None:
                    entry.word_count = chapter.word_count

        classifications = classify_chapters(chapters)
        for toc_entry in manifest.structure.toc:
            result = classifications[toc_entry.id]
            toc_entry.kind = result.kind
            toc_entry.kind_confidence = result.confidence
            toc_entry.kind_reason = result.reason

        run.save_book(manifest)
        for chapter in chapters:
            run.save_raw_chapter(chapter)

        emit(StageFinish(stage=_STAGE, summary=f"ingested {len(chapters)} chapters"))

        return IngestResult(
            run_dir=run.root,
            manifest=manifest,
            chapters=chapters,
            is_pdf=is_pdf,
            cleaned=do_clean,
            cleanup=cleanup_stats,
        )

    @staticmethod
    def _run_cleanup(
        chapters: list[NormalizedChapter],
        *,
        concurrency: int,
        client: Any | None,
        emit: ProgressCallback,
    ) -> tuple[list[NormalizedChapter], CleanupStats]:
        emit(StageStart(stage=_CLEANUP_STAGE, total=len(chapters)))

        def on_progress(chapter: NormalizedChapter) -> None:
            emit(ItemDone(stage=_CLEANUP_STAGE, item_id=chapter.chapter_id, status="ok"))

        def on_failure(chapter: NormalizedChapter, exc: Exception) -> None:
            emit(
                ItemDone(
                    stage=_CLEANUP_STAGE,
                    item_id=chapter.chapter_id,
                    status="fail",
                    detail=str(exc),
                )
            )

        cleaned, total_in, total_out, failed_ids = asyncio.run(
            clean_chapters_async(
                chapters,
                client=client,
                concurrency=concurrency,
                on_progress=on_progress,
                on_failure=on_failure,
            )
        )

        stats = CleanupStats(
            input_tokens=total_in,
            output_tokens=total_out,
            cost_usd=_sonnet_cost_usd(total_in, total_out),
            failed_ids=failed_ids,
        )

        ok_count = len(cleaned) - len(failed_ids)
        emit(
            StageFinish(
                stage=_CLEANUP_STAGE,
                summary=f"{ok_count}/{len(cleaned)} cleaned · ${stats.cost_usd:.2f}",
            )
        )
        if failed_ids:
            emit(
                WarningEvent(
                    stage=_CLEANUP_STAGE,
                    message=f"{len(failed_ids)} chapter(s) failed cleanup: "
                    f"{', '.join(failed_ids)} — kept Tier 2 output",
                )
            )
        return cleaned, stats


__all__ = [
    "CleanupStats",
    "IngestInput",
    "IngestResult",
    "IngestService",
    "MetadataConfirmHook",
]
