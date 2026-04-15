"""Plan stage service — design the plugin from distilled sidecars.

Loads the book + sidecars, calls the planner, saves ``plan.json``.
A single LLM call, so progress is just stage_start/stage_finish with
no per-item events.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from franklin.checkpoint import RunDirectory
from franklin.estimate import _opus_cost
from franklin.planner import DEFAULT_MODEL, build_user_prompt, design_plan
from franklin.schema import BookManifest, ChapterSidecar, PlanManifest
from franklin.services.events import (
    ProgressCallback,
    StageFinish,
    StageStart,
)
from franklin.services.map import RunNotIngestedError

_STAGE = "plan"


class NoSidecarsError(RuntimeError):
    """The run directory has no sidecars — map hasn't run yet."""


class PlanAlreadyExistsError(RuntimeError):
    """``plan.json`` is already present and ``force`` wasn't set."""

    def __init__(self, plan_path: Path) -> None:
        self.plan_path = plan_path
        super().__init__(f"plan.json already exists at {plan_path}; use force=True to regenerate")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanInput(_Base):
    run_dir: Path
    model: str = DEFAULT_MODEL
    force: bool = False


class PlanContext(_Base):
    """Everything the CLI needs to render a dry run.

    Returned by ``PlanService.prepare`` so ``--dry-run`` can print the
    prompt without touching the LLM and without the CLI re-implementing
    the load/validate dance the service already does.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run: RunDirectory
    manifest: BookManifest
    sidecars: list[ChapterSidecar]


class PlanResult(_Base):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_dir: Path
    plan: PlanManifest
    input_tokens: int
    output_tokens: int
    cost_usd: float


class PlanService:
    def prepare(self, params: PlanInput) -> PlanContext:
        """Load book + sidecars and validate preconditions.

        Raises ``RunNotIngestedError`` if ``book.json`` is missing,
        ``NoSidecarsError`` if the chapters directory is empty, and
        ``PlanAlreadyExistsError`` if ``plan.json`` is present and
        ``force`` is not set.
        """
        run = RunDirectory(params.run_dir)
        if not run.book_json.exists():
            raise RunNotIngestedError(
                f"no book.json in {params.run_dir} — run `franklin ingest` first"
            )

        sidecar_ids = [p.stem for p in sorted(run.chapters_dir.glob("*.json"))]
        if not sidecar_ids:
            raise NoSidecarsError(f"no sidecars in {run.chapters_dir} — run `franklin map` first")

        if run.plan_json.exists() and not params.force:
            raise PlanAlreadyExistsError(run.plan_json)

        manifest = run.load_book()
        sidecars = [run.load_sidecar(cid) for cid in sidecar_ids]
        return PlanContext(run=run, manifest=manifest, sidecars=sidecars)

    def build_prompt(self, manifest: BookManifest, sidecars: list[ChapterSidecar]) -> str:
        """Render the planner user prompt without calling the LLM."""
        return build_user_prompt(manifest, sidecars)

    def run(
        self,
        params: PlanInput,
        *,
        progress: ProgressCallback | None = None,
        client: Any | None = None,
    ) -> PlanResult:
        emit = progress or (lambda _event: None)
        context = self.prepare(params)

        emit(StageStart(stage=_STAGE))
        plan, in_toks, out_toks = design_plan(
            context.manifest,
            context.sidecars,
            client=client,
            model=params.model,
        )
        context.run.save_plan(plan)

        cost = _opus_cost(in_toks, out_toks)
        context.run.append_cost(
            stage=_STAGE,
            model=params.model,
            input_tokens=in_toks,
            output_tokens=out_toks,
            cost_usd=cost,
        )

        emit(
            StageFinish(
                stage=_STAGE,
                summary=f"{in_toks:,} in / {out_toks:,} out · ${cost:.2f}",
            )
        )

        return PlanResult(
            run_dir=context.run.root,
            plan=plan,
            input_tokens=in_toks,
            output_tokens=out_toks,
            cost_usd=cost,
        )


__all__ = [
    "NoSidecarsError",
    "PlanAlreadyExistsError",
    "PlanContext",
    "PlanInput",
    "PlanResult",
    "PlanService",
]
