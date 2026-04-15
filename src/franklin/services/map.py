"""Map stage service — per-chapter structured extraction via the LLM.

Drives ``extract_chapter_async`` concurrently across chapters, emitting
ProgressEvents that the CLI translates into a Rich progress bar. The
service loads chapter selection from ``map_selection.json`` (if
present) and skips already-extracted chapters unless ``force=True``.

The ``--dry-run`` path stays in the CLI since it never calls the LLM;
``build_dry_run_prompt`` is exposed as a helper so the CLI can render
the prompt for a single chapter without re-implementing selection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from franklin.checkpoint import RunDirectory
from franklin.llm import make_async_client
from franklin.mapper import DEFAULT_MODEL, build_user_prompt, extract_chapter_async
from franklin.schema import BookManifest, ChapterKind, NormalizedChapter
from franklin.services.events import (
    InfoEvent,
    ItemDone,
    ItemStart,
    ProgressCallback,
    StageFinish,
    StageStart,
)

_STAGE = "map"


def _sonnet_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * 3.0 + (output_tokens / 1_000_000) * 15.0


class ChapterNotFoundError(LookupError):
    """The requested chapter isn't present in the run directory."""


class RunNotIngestedError(RuntimeError):
    """The run directory lacks a book.json — ingest hasn't run yet."""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MapInput(_Base):
    run_dir: Path
    chapter_id: str | None = None
    model: str = DEFAULT_MODEL
    force: bool = False
    concurrency: int = Field(default=8, ge=1, le=32)


class MapResult(_Base):
    run_dir: Path
    extracted_count: int
    skipped_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class TargetSelection(_Base):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run: RunDirectory
    manifest: BookManifest
    targets: list[NormalizedChapter]
    # When a map_selection.json file narrows the chapter list, record
    # how many of the eligible content chapters the selection kept so
    # the CLI can surface "N/M chapters" without reloading the manifest.
    selection_kept: int | None = None
    selection_total: int | None = None


class MapService:
    """Map a run directory's raw chapters into structured sidecars."""

    def select_targets(self, params: MapInput) -> TargetSelection:
        """Resolve which chapters this invocation will extract.

        Raises ``RunNotIngestedError`` if ``book.json`` is missing and
        ``ChapterNotFoundError`` if the caller pinned a chapter_id that
        wasn't ingested.
        """
        run = RunDirectory(params.run_dir)
        if not run.book_json.exists():
            raise RunNotIngestedError(
                f"no book.json in {params.run_dir} — run `franklin ingest` first"
            )

        manifest = run.load_book()

        if params.chapter_id is not None:
            raw_path = run.raw_chapter_path(params.chapter_id)
            if not raw_path.exists():
                raise ChapterNotFoundError(
                    f"chapter {params.chapter_id} not found in {run.raw_dir}"
                )
            return TargetSelection(
                run=run,
                manifest=manifest,
                targets=[run.load_raw_chapter(params.chapter_id)],
            )

        content_ids = [
            entry.id
            for entry in manifest.structure.toc
            if entry.kind in (ChapterKind.CONTENT, ChapterKind.INTRODUCTION)
        ]
        total_content = len(content_ids)

        selection = run.load_map_selection()
        selection_kept: int | None = None
        if selection is not None:
            allowed = set(selection)
            filtered = [cid for cid in content_ids if cid in allowed]
            if filtered:
                selection_kept = len(filtered)
                content_ids = filtered

        return TargetSelection(
            run=run,
            manifest=manifest,
            targets=[run.load_raw_chapter(cid) for cid in content_ids],
            selection_kept=selection_kept,
            selection_total=total_content if selection_kept is not None else None,
        )

    def build_dry_run_prompt(self, manifest: BookManifest, chapter: NormalizedChapter) -> str:
        """Render the user prompt for a chapter without calling the LLM."""
        return build_user_prompt(manifest, chapter)

    def run(
        self,
        params: MapInput,
        *,
        progress: ProgressCallback | None = None,
        client: Any | None = None,
    ) -> MapResult:
        emit = progress or (lambda _event: None)

        selection = self.select_targets(params)
        run = selection.run
        manifest = selection.manifest
        targets = selection.targets

        if selection.selection_kept is not None:
            emit(
                InfoEvent(
                    stage=_STAGE,
                    message=f"using chapter selection from map_selection.json "
                    f"({selection.selection_kept}/{selection.selection_total} chapters)",
                )
            )

        to_extract: list[NormalizedChapter] = []
        skipped = 0
        for chapter in targets:
            if run.sidecar_path(chapter.chapter_id).exists() and not params.force:
                skipped += 1
            else:
                to_extract.append(chapter)

        if skipped:
            emit(
                InfoEvent(
                    stage=_STAGE,
                    message=f"{skipped} chapter(s) already extracted, skipping",
                )
            )

        if not to_extract:
            emit(
                StageFinish(
                    stage=_STAGE,
                    summary=f"0 extracted, {skipped} skipped",
                )
            )
            return MapResult(
                run_dir=run.root,
                extracted_count=0,
                skipped_count=skipped,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
            )

        emit(StageStart(stage=_STAGE, total=len(to_extract)))

        llm = client if client is not None else make_async_client()
        total_in, total_out, extracted = self._extract_concurrently(
            manifest=manifest,
            run=run,
            to_extract=to_extract,
            client=llm,
            model=params.model,
            concurrency=params.concurrency,
            emit=emit,
        )

        cost = _sonnet_cost_usd(total_in, total_out)
        run.append_cost(
            stage=_STAGE,
            model=params.model,
            input_tokens=total_in,
            output_tokens=total_out,
            cost_usd=cost,
        )
        emit(
            StageFinish(
                stage=_STAGE,
                summary=f"{extracted} extracted, {skipped} skipped · "
                f"{total_in:,} in / {total_out:,} out · ${cost:.2f}",
            )
        )

        return MapResult(
            run_dir=run.root,
            extracted_count=extracted,
            skipped_count=skipped,
            input_tokens=total_in,
            output_tokens=total_out,
            cost_usd=cost,
        )

    @staticmethod
    def _extract_concurrently(
        *,
        manifest: BookManifest,
        run: RunDirectory,
        to_extract: list[NormalizedChapter],
        client: Any,
        model: str,
        concurrency: int,
        emit: ProgressCallback,
    ) -> tuple[int, int, int]:
        total_in = 0
        total_out = 0
        extracted = 0

        async def run_all() -> None:
            nonlocal total_in, total_out, extracted
            sem = asyncio.Semaphore(concurrency)

            async def one(chapter: NormalizedChapter) -> None:
                nonlocal total_in, total_out, extracted
                async with sem:
                    emit(ItemStart(stage=_STAGE, item_id=chapter.chapter_id))
                    sidecar, in_toks, out_toks = await extract_chapter_async(
                        manifest, chapter, model=model, client=client
                    )
                    run.save_sidecar(sidecar)
                    total_in += in_toks
                    total_out += out_toks
                    extracted += 1
                    emit(
                        ItemDone(
                            stage=_STAGE,
                            item_id=chapter.chapter_id,
                            status="ok",
                            detail=f"{len(sidecar.concepts)}c/{len(sidecar.rules)}r",
                        )
                    )

            await asyncio.gather(*(one(ch) for ch in to_extract), return_exceptions=False)

        asyncio.run(run_all())
        return total_in, total_out, extracted


__all__ = [
    "ChapterNotFoundError",
    "MapInput",
    "MapResult",
    "MapService",
    "RunNotIngestedError",
    "TargetSelection",
]
