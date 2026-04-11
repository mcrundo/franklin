"""Interactive plan review — pure transformations only.

``franklin review <run-dir>`` pauses between the plan and reduce stages
so the user can look at the proposed artifact list and omit any they
don't want to pay to generate. Reduce is the most expensive stage by
a wide margin, so a cheap review pass catches "the planner decided to
generate a tutorial I don't want" before spending tens of dollars.

Everything in this module is pure data transformation; the CLI layer
owns the prompting, rendering, and disk writes. That split keeps the
test surface small.
"""

from __future__ import annotations

from dataclasses import dataclass

from franklin.schema import Artifact, PlanManifest


@dataclass(frozen=True)
class ReviewResult:
    """Outcome of a plan-review pass."""

    plan: PlanManifest
    omitted: list[Artifact]

    @property
    def omitted_ids(self) -> list[str]:
        return [a.id for a in self.omitted]

    @property
    def kept_count(self) -> int:
        return len(self.plan.artifacts)


def apply_omissions(plan: PlanManifest, omit_ids: list[str]) -> ReviewResult:
    """Return a new plan with the listed artifact ids removed.

    Unknown ids are ignored rather than raising — the CLI layer is
    expected to validate input before calling, but silently tolerating
    duplicates and typos is less frustrating in practice.
    """
    omit_set = set(omit_ids)
    kept: list[Artifact] = []
    omitted: list[Artifact] = []
    for artifact in plan.artifacts:
        if artifact.id in omit_set:
            omitted.append(artifact)
        else:
            kept.append(artifact)

    new_plan = plan.model_copy(
        update={
            "artifacts": kept,
            "estimated_reduce_calls": len(kept),
            "estimated_total_output_tokens": sum(a.estimated_output_tokens for a in kept),
        }
    )
    return ReviewResult(plan=new_plan, omitted=omitted)


def parse_omit_selection(raw: str, total: int) -> list[int]:
    """Parse a comma/space-separated numeric selection into a sorted index list.

    Accepts ``"1,3, 5"`` and ``"1 3 5"``. Ranges (``1-3``) are supported.
    Out-of-bounds or non-numeric tokens raise ``ValueError`` with a
    human-friendly message; the CLI catches it and re-prompts.
    """
    if not raw.strip():
        return []
    tokens = [t for t in raw.replace(",", " ").split() if t]
    indices: set[int] = set()
    for token in tokens:
        if "-" in token:
            start_s, _, end_s = token.partition("-")
            try:
                start, end = int(start_s), int(end_s)
            except ValueError as exc:
                raise ValueError(f"invalid range {token!r}") from exc
            if start > end:
                raise ValueError(f"range {token!r} has start > end")
            for i in range(start, end + 1):
                indices.add(i)
        else:
            try:
                indices.add(int(token))
            except ValueError as exc:
                raise ValueError(f"{token!r} is not a number") from exc
    for i in indices:
        if i < 1 or i > total:
            raise ValueError(f"index {i} is out of range (1-{total})")
    return sorted(indices)
