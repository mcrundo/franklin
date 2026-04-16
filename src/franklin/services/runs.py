"""Runs service — list, detail, and cost queries for run directories.

Wraps the checkpoint / grading primitives into Pydantic models that a
web API can serialize directly. The diagnostics CLI commands (``stats``,
``costs``, ``runs list``) currently build their own report structures
inline; once this service exists they can optionally delegate here.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from franklin.checkpoint import RunDirectory, RunSummary, list_runs, summarize_run
from franklin.grading import RunGrade, grade_run


class CostEntry(BaseModel):
    """One cost-tracking line from costs.json."""

    model_config = ConfigDict(extra="allow")

    stage: str = "unknown"
    model: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class RunDetail(BaseModel):
    """Full detail for a single run — summary + grade + costs.

    Designed as the response body for ``GET /api/runs/:id``.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    summary: RunSummary
    grade: RunGrade | None = None
    costs: list[CostEntry]
    total_cost_usd: float


class CostReportEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str | None
    cost_usd: float


class StageCostBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    cost_usd: float


class CostReport(BaseModel):
    """Aggregate cost report across runs.

    Designed as the response body for ``GET /api/costs``.
    """

    model_config = ConfigDict(extra="forbid")

    runs: list[CostReportEntry]
    by_stage: list[StageCostBreakdown]
    grand_total_usd: float


class RunsService:
    """Read-only queries against run directories. No LLM, no mutations."""

    def list(self, base: Path) -> list[RunSummary]:
        """List every run under ``base``, newest first."""
        return list_runs(base)

    def get(self, run_dir: Path) -> RunDetail:
        """Full detail for one run: summary + grade + cost breakdown."""
        summary = summarize_run(run_dir)
        run = RunDirectory(run_dir)

        import contextlib

        grade: RunGrade | None = None
        if summary.last_stage == "assemble":
            with contextlib.suppress(Exception):
                grade = grade_run(run_dir)

        raw_costs = run.load_costs()
        costs = [CostEntry.model_validate(e) for e in raw_costs]
        total = sum(c.cost_usd for c in costs)

        return RunDetail(
            summary=summary,
            grade=grade,
            costs=costs,
            total_cost_usd=total,
        )

    def costs(self, base: Path) -> CostReport:
        """Aggregate cost report across all runs under ``base``."""
        summaries = list_runs(base)

        runs: list[CostReportEntry] = []
        stage_totals: dict[str, float] = {}

        for s in summaries:
            run = RunDirectory(s.path)
            entries = run.load_costs()
            if not entries:
                continue
            run_cost = sum(float(str(e.get("cost_usd", 0))) for e in entries)
            runs.append(CostReportEntry(slug=s.slug, title=s.title, cost_usd=run_cost))
            for e in entries:
                stage = str(e.get("stage", "unknown"))
                stage_totals[stage] = stage_totals.get(stage, 0.0) + float(
                    str(e.get("cost_usd", 0))
                )

        grand_total = sum(r.cost_usd for r in runs)
        by_stage = [
            StageCostBreakdown(stage=s, cost_usd=c)
            for s, c in sorted(stage_totals.items(), key=lambda x: -x[1])
        ]

        return CostReport(
            runs=runs,
            by_stage=by_stage,
            grand_total_usd=grand_total,
        )


__all__ = [
    "CostEntry",
    "CostReport",
    "CostReportEntry",
    "RunDetail",
    "RunsService",
    "StageCostBreakdown",
]
