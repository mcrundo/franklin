"""Plan stage: design a plugin architecture from distilled sidecars.

A single high-leverage call reads every ChapterSidecar and proposes a
PlanManifest: which skill, references, commands, and agents to generate,
which artifact types to skip, and the high-level rationale for the shape.

The map stage's broad per-chapter extraction feeds this focused planning
call — the best place in the pipeline to spend Opus tokens.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from franklin.checkpoint import slugify
from franklin.llm import call_tool, make_client, render_prompt, validate_with_extra_recovery
from franklin.llm.client import DEFAULT_MAX_TOKENS
from franklin.llm.models import PLAN_MODEL
from franklin.schema import (
    BookManifest,
    ChapterSidecar,
    PlanManifest,
    PlanProposal,
)

DEFAULT_MODEL = PLAN_MODEL

_TOOL_NAME = "save_plan_proposal"
_TOOL_DESCRIPTION = (
    "Persist the proposed Claude Code plugin architecture. Call this tool "
    "exactly once with a complete PlanProposal describing every artifact "
    "to generate, the skipped artifact types with reasons, the coherence "
    "rules, and your planner_rationale."
)

_SYSTEM_PROMPT = (
    "You are an experienced plugin architect designing Claude Code plugins "
    "from technical books. You are opinionated about what each primitive "
    "(skill, reference, command, agent) is for, and you only propose "
    "artifacts the source material genuinely supports. You let the book's "
    "own shape drive the plugin's shape rather than forcing a template. "
    "You always respond by calling the tool you are given, never with prose."
)


def design_plan(
    book: BookManifest,
    sidecars: list[ChapterSidecar],
    *,
    model: str = DEFAULT_MODEL,
    client: Any | None = None,
    max_tokens: int | None = None,
) -> tuple[PlanManifest, int, int]:
    """Run the plan stage: propose a PlanManifest for the whole book.

    Returns the merged PlanManifest and input/output token counts.

    Raises RuntimeError on tool-use failure and if the returned payload
    does not validate against PlanProposal.
    """
    llm = client if client is not None else make_client()
    user_prompt = build_user_prompt(book, sidecars)
    tool_schema = build_tool_schema()

    result = call_tool(
        client=llm,
        model=model,
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
        tool_schema=tool_schema,
        max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
    )

    try:
        proposal = validate_with_extra_recovery(
            PlanProposal,
            result.input,
            label="planner",
        )
    except ValidationError as exc:
        raise RuntimeError(f"planner returned invalid proposal: {exc}") from exc

    plan = PlanManifest.from_proposal(
        proposal,
        book_id=slugify(book.metadata.title),
        planner_model=model,
    )
    return plan, result.input_tokens, result.output_tokens


def build_user_prompt(book: BookManifest, sidecars: list[ChapterSidecar]) -> str:
    """Render the plan prompt with the full distilled book."""
    return render_prompt(
        "plan_plugin",
        distilled_book=build_distilled_view(book, sidecars),
    )


def build_tool_schema() -> dict[str, Any]:
    """Pydantic-derived JSON schema for the save_plan_proposal tool."""
    return PlanProposal.model_json_schema()


def build_distilled_view(book: BookManifest, sidecars: list[ChapterSidecar]) -> str:
    """Render the sidecars as a compact markdown view for the planner.

    Full code example bodies are omitted — the planner only needs to know
    code examples exist and what they illustrate, not the code itself.
    That keeps the distilled view small enough to fit comfortably in a
    single planning call.
    """
    parts: list[str] = []
    parts.append(f"# {book.metadata.title}")
    if book.metadata.authors:
        parts.append(f"**Authors:** {', '.join(book.metadata.authors)}")
    parts.append(f"**Chapters in scope:** {len(sidecars)}")
    parts.append("")

    for sc in sidecars:
        parts.append(f"## {sc.chapter_id}: {sc.title}")
        parts.append("")
        parts.append(f"**Summary.** {sc.summary}")
        parts.append("")

        if sc.concepts:
            parts.append("**Concepts:**")
            for c in sc.concepts:
                parts.append(f"- `{c.id}` **{c.name}** ({c.importance.value}) — {c.definition}")
            parts.append("")

        if sc.principles:
            parts.append("**Principles:**")
            for p in sc.principles:
                parts.append(f"- `{p.id}` {p.statement}")
            parts.append("")

        if sc.rules:
            parts.append("**Rules:**")
            for r in sc.rules:
                parts.append(f"- `{r.id}` {r.rule}")
            parts.append("")

        if sc.anti_patterns:
            parts.append("**Anti-patterns:**")
            for a in sc.anti_patterns:
                parts.append(f"- `{a.id}` **{a.name}** — {a.description}")
            parts.append("")

        if sc.decision_rules:
            parts.append("**Decision rules:**")
            for d in sc.decision_rules:
                parts.append(f"- `{d.id}` {d.question}")
            parts.append("")

        if sc.actionable_workflows:
            parts.append("**Actionable workflows:**")
            for w in sc.actionable_workflows:
                parts.append(f"- `{w.id}` {w.name}")
            parts.append("")

        if sc.code_examples:
            preview_labels = [c.label for c in sc.code_examples[:8]]
            more = f" (+ {len(sc.code_examples) - 8} more)" if len(sc.code_examples) > 8 else ""
            parts.append(
                f"**Code examples:** {len(sc.code_examples)} — {', '.join(preview_labels)}{more}"
            )
            parts.append("")

        if sc.terminology:
            terms = ", ".join(t.term for t in sc.terminology)
            parts.append(f"**Terminology:** {terms}")
            parts.append("")

        if sc.cross_references:
            refs = "; ".join(f"{cr.to_chapter}: {cr.reason}" for cr in sc.cross_references)
            parts.append(f"**Cross-references:** {refs}")
            parts.append("")

    return "\n".join(parts)
