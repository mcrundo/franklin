"""Reduce stage service — generate each plan artifact from its filtered slice.

Per-artifact async generation with a bounded semaphore. Failures are
non-fatal: a single artifact that raises is recorded in the result's
``failed_count`` and ``ItemDone(status="fail")`` is emitted, but the
rest of the batch continues. This matches the current CLI behavior.

The ``fix`` command pre-resolves its own target list from a grade
report; it reaches into ``generate`` directly rather than going
through ``run``, so the selection logic is a separate public method.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from franklin.checkpoint import RunDirectory
from franklin.llm import make_async_client
from franklin.reducer import DEFAULT_MODEL, generate_artifact_async
from franklin.schema import (
    Artifact,
    ArtifactType,
    BookManifest,
    ChapterSidecar,
    PlanManifest,
)
from franklin.services.events import (
    ItemDone,
    ItemStart,
    ProgressCallback,
    StageFinish,
    StageStart,
)

_STAGE = "reduce"


def _sonnet_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * 3.0 + (output_tokens / 1_000_000) * 15.0


class NoPlanError(RuntimeError):
    """The run directory lacks a plan.json."""


class NoSidecarsForReduceError(RuntimeError):
    """The run directory has no sidecars."""


class ArtifactNotFoundError(LookupError):
    """Caller asked for an artifact id the plan doesn't define."""

    def __init__(self, artifact_id: str, available: list[str]) -> None:
        self.artifact_id = artifact_id
        self.available = available
        super().__init__(
            f"no artifact with id {artifact_id!r} in plan (available: {', '.join(available)})"
        )


class UnknownArtifactTypeError(ValueError):
    """Caller passed a --type that isn't a valid ArtifactType."""

    def __init__(self, requested: str, valid: list[str]) -> None:
        self.requested = requested
        self.valid = valid
        super().__init__(f"unknown artifact type {requested!r} (valid: {', '.join(valid)})")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReduceInput(_Base):
    run_dir: Path
    artifact_id: str | None = None
    type_filter: str | None = None
    model: str = DEFAULT_MODEL
    force: bool = False
    concurrency: int = Field(default=3, ge=1, le=16)


class ReduceContext(_Base):
    """Everything downstream needs to generate artifacts.

    Returned by ``prepare``; also constructable by callers like ``fix``
    that already have plan/book/sidecars loaded and want to skip
    reading them again.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run: RunDirectory
    plan: PlanManifest
    book: BookManifest
    sidecars: dict[str, ChapterSidecar]


class ReduceResult(_Base):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_dir: Path
    plugin_root: Path
    generated_count: int
    skipped_count: int
    failed_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float


class ReduceService:
    def prepare(self, params: ReduceInput) -> ReduceContext:
        run = RunDirectory(params.run_dir)
        if not run.plan_json.exists():
            raise NoPlanError(f"no plan.json in {params.run_dir} — run `franklin plan` first")

        sidecar_ids = [p.stem for p in sorted(run.chapters_dir.glob("*.json"))]
        if not sidecar_ids:
            raise NoSidecarsForReduceError(
                f"no sidecars in {run.chapters_dir} — run `franklin map` first"
            )

        return ReduceContext(
            run=run,
            plan=run.load_plan(),
            book=run.load_book(),
            sidecars={cid: run.load_sidecar(cid) for cid in sidecar_ids},
        )

    def select_artifacts(
        self,
        plan: PlanManifest,
        *,
        artifact_id: str | None = None,
        type_filter: str | None = None,
    ) -> list[Artifact]:
        """Filter the plan's artifact list by id or type.

        Either ``artifact_id`` or ``type_filter`` may be set (the CLI
        exposes them as distinct flags but only one is expected at a
        time). With both ``None``, returns every artifact.
        """
        if artifact_id is not None:
            for art in plan.artifacts:
                if art.id == artifact_id:
                    return [art]
            raise ArtifactNotFoundError(artifact_id, [a.id for a in plan.artifacts])

        if type_filter is not None:
            try:
                kind = ArtifactType(type_filter)
            except ValueError as exc:
                raise UnknownArtifactTypeError(
                    type_filter, [t.value for t in ArtifactType]
                ) from exc
            return [a for a in plan.artifacts if a.type == kind]

        return list(plan.artifacts)

    async def generate_async(
        self,
        context: ReduceContext,
        targets: list[Artifact],
        *,
        model: str = DEFAULT_MODEL,
        force: bool = False,
        concurrency: int = 3,
        progress: ProgressCallback | None = None,
        client: Any | None = None,
    ) -> ReduceResult:
        """Async implementation — safe to call from an existing event loop.

        The sync ``generate`` delegates here via ``asyncio.run``.
        Used both by ``run`` and by the ``fix`` CLI command which
        pre-resolves its targets from a grade report.
        """
        emit = progress or (lambda _event: None)
        output_root = context.run.output_dir / context.plan.plugin.name
        output_root.mkdir(parents=True, exist_ok=True)

        to_generate: list[Artifact] = []
        skipped = 0
        for artifact in targets:
            out_path = output_root / artifact.path
            if out_path.exists() and not force:
                skipped += 1
            else:
                to_generate.append(artifact)

        if not to_generate:
            emit(
                StageFinish(
                    stage=_STAGE,
                    summary=f"0 generated, {skipped} skipped, 0 failed",
                )
            )
            return ReduceResult(
                run_dir=context.run.root,
                plugin_root=output_root,
                generated_count=0,
                skipped_count=skipped,
                failed_count=0,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=0.0,
            )

        emit(StageStart(stage=_STAGE, total=len(to_generate)))
        llm = client if client is not None else make_async_client()

        totals = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "generated": 0,
            "failed": 0,
        }
        sem = asyncio.Semaphore(concurrency)

        async def one(artifact: Artifact) -> None:
            async with sem:
                emit(
                    ItemStart(
                        stage=_STAGE,
                        item_id=artifact.id,
                        label=f"{artifact.type.value}:{artifact.id}",
                    )
                )
                try:
                    result = await generate_artifact_async(
                        artifact,
                        plan=context.plan,
                        book=context.book,
                        sidecars=context.sidecars,
                        client=llm,
                        model=model,
                    )
                except Exception as exc:  # non-fatal: record + keep going
                    totals["failed"] += 1
                    emit(
                        ItemDone(
                            stage=_STAGE,
                            item_id=artifact.id,
                            status="fail",
                            detail=str(exc),
                        )
                    )
                    return

                out_path = output_root / artifact.path
                out_path.parent.mkdir(parents=True, exist_ok=True)
                body = result.content if result.content.endswith("\n") else result.content + "\n"
                out_path.write_text(body)

                totals["input"] += result.input_tokens
                totals["output"] += result.output_tokens
                totals["cache_read"] += result.cache_read_tokens
                totals["cache_creation"] += result.cache_creation_tokens
                totals["generated"] += 1
                emit(
                    ItemDone(
                        stage=_STAGE,
                        item_id=artifact.id,
                        status="ok",
                        detail=f"{len(result.content):,} chars",
                    )
                )

        await asyncio.gather(*(one(a) for a in to_generate))

        cost = _sonnet_cost_usd(totals["input"], totals["output"])
        context.run.append_cost(
            stage=_STAGE,
            model=model,
            input_tokens=totals["input"],
            output_tokens=totals["output"],
            cache_read_tokens=totals["cache_read"],
            cost_usd=cost,
        )

        emit(
            StageFinish(
                stage=_STAGE,
                summary=f"{totals['generated']} generated, {skipped} skipped, "
                f"{totals['failed']} failed · "
                f"{totals['input']:,} in / {totals['output']:,} out / "
                f"{totals['cache_read']:,} cache-read · ${cost:.2f}",
            )
        )

        return ReduceResult(
            run_dir=context.run.root,
            plugin_root=output_root,
            generated_count=totals["generated"],
            skipped_count=skipped,
            failed_count=totals["failed"],
            input_tokens=totals["input"],
            output_tokens=totals["output"],
            cache_read_tokens=totals["cache_read"],
            cache_creation_tokens=totals["cache_creation"],
            cost_usd=cost,
        )

    def generate(
        self,
        context: ReduceContext,
        targets: list[Artifact],
        *,
        model: str = DEFAULT_MODEL,
        force: bool = False,
        concurrency: int = 3,
        progress: ProgressCallback | None = None,
        client: Any | None = None,
    ) -> ReduceResult:
        """Sync wrapper — calls ``generate_async`` via ``asyncio.run``."""
        return asyncio.run(
            self.generate_async(
                context,
                targets,
                model=model,
                force=force,
                concurrency=concurrency,
                progress=progress,
                client=client,
            )
        )

    def run(
        self,
        params: ReduceInput,
        *,
        progress: ProgressCallback | None = None,
        client: Any | None = None,
    ) -> ReduceResult:
        context = self.prepare(params)
        targets = self.select_artifacts(
            context.plan,
            artifact_id=params.artifact_id,
            type_filter=params.type_filter,
        )
        return self.generate(
            context,
            targets,
            model=params.model,
            force=params.force,
            concurrency=params.concurrency,
            progress=progress,
            client=client,
        )


__all__ = [
    "ArtifactNotFoundError",
    "NoPlanError",
    "NoSidecarsForReduceError",
    "ReduceContext",
    "ReduceInput",
    "ReduceResult",
    "ReduceService",
    "UnknownArtifactTypeError",
]
