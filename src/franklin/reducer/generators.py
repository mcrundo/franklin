"""Per-artifact file generators for the reduce stage.

One entry point — `generate_artifact` — dispatches on the artifact type to
pick the right prompt template, assembles the cached/variable content
block split, and calls the LLM with forced tool-use. All four artifact
types share the same tool (`save_artifact_file` with a `{content: str}`
schema); the differences live in the per-type prompt templates and in
the extra context each type needs injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from franklin.llm import (
    cached_text_block,
    call_tool,
    make_client,
    render_prompt,
    text_block,
)
from franklin.llm.client import DEFAULT_MAX_TOKENS
from franklin.reducer.resolver import ResolvedContext, resolve_feeds
from franklin.schema import (
    Artifact,
    ArtifactType,
    BookManifest,
    ChapterSidecar,
    PlanManifest,
)

DEFAULT_MODEL = "claude-sonnet-4-6"
CACHE_BREAKPOINT = "<!-- CACHE-BREAKPOINT -->"

_TOOL_NAME = "save_artifact_file"
_TOOL_DESCRIPTION = (
    "Persist one generated artifact file. Call this tool exactly once with "
    "the full file contents (including any YAML frontmatter) in the content "
    "field. Do not reply with any prose outside the tool call."
)
_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": (
                "The complete file contents to write to disk, including "
                "any YAML frontmatter and trailing newline."
            ),
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You generate individual files for Claude Code plugins derived from "
    "technical books. You are faithful to the source material: you quote "
    "concepts, definitions, and code examples verbatim when the provided "
    "sidecar data has them, and you do not invent content the sidecars do "
    "not support. You always respond by calling the save_artifact_file "
    "tool exactly once; you never reply with prose."
)


@dataclass(frozen=True)
class GenerationResult:
    """Output of generating one artifact file."""

    content: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


def generate_artifact(
    artifact: Artifact,
    *,
    plan: PlanManifest,
    book: BookManifest,
    sidecars: dict[str, ChapterSidecar],
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
) -> GenerationResult:
    """Generate one artifact file via a cached tool-use call.

    Resolves the artifact's `feeds_from` into a filtered sidecar slice,
    renders the type-appropriate prompt template, splits on the cache
    breakpoint marker into a cached prefix and a variable tail, and calls
    the LLM with forced tool-use returning `{content: str}`. The
    returned content is the file body ready to write to disk.
    """
    llm = client if client is not None else make_client()

    context = resolve_feeds(artifact.feeds_from, book=book, sidecars=sidecars)

    template_name = _template_name_for(artifact.type)
    template_vars = _build_template_vars(
        artifact=artifact, plan=plan, context=context
    )
    rendered = render_prompt(template_name, **template_vars)

    if CACHE_BREAKPOINT not in rendered:
        raise RuntimeError(
            f"prompt template {template_name!r} is missing the CACHE-BREAKPOINT marker"
        )
    prefix, suffix = rendered.split(CACHE_BREAKPOINT, maxsplit=1)

    user_content: list[dict[str, Any]] = [
        cached_text_block(prefix.rstrip()),
        text_block(suffix.lstrip()),
    ]
    system_content: list[dict[str, Any]] = [cached_text_block(_SYSTEM_PROMPT)]

    result = call_tool(
        client=llm,
        model=model,
        system=system_content,
        user=user_content,
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
        tool_schema=_TOOL_SCHEMA,
        max_tokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
    )

    content = result.input.get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            f"generator returned empty or non-string content for {artifact.path}"
        )

    return GenerationResult(
        content=content,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
    )


# ---------------------------------------------------------------------------
# Template selection and variable assembly
# ---------------------------------------------------------------------------


_TEMPLATE_BY_TYPE: dict[ArtifactType, str] = {
    ArtifactType.SKILL: "generate_skill",
    ArtifactType.REFERENCE: "generate_reference",
    ArtifactType.COMMAND: "generate_command",
    ArtifactType.AGENT: "generate_agent",
}


def _template_name_for(artifact_type: ArtifactType) -> str:
    if artifact_type not in _TEMPLATE_BY_TYPE:
        raise ValueError(f"no generator for artifact type: {artifact_type}")
    return _TEMPLATE_BY_TYPE[artifact_type]


def _build_template_vars(
    *, artifact: Artifact, plan: PlanManifest, context: ResolvedContext
) -> dict[str, str]:
    coherence_rules = (
        "\n".join(f"- {rule}" for rule in plan.coherence_rules)
        if plan.coherence_rules
        else "_(no coherence rules specified)_"
    )

    book_context = context.book_markdown or f"# {plan.plugin.name}"
    resolved_context = (
        context.chapters_markdown or "_(no sidecar slice resolved)_"
    )

    # plan_tree is included for every artifact type so generators can emit
    # correct relative links between files. It lives in the cached prefix
    # (it's stable across every call in a run), which has the side benefit
    # of pushing the cached block past Anthropic's 1024-token minimum for
    # eligibility so prompt caching actually fires on repeated calls.
    template_vars: dict[str, str] = {
        "coherence_rules": coherence_rules,
        "book_context": book_context,
        "artifact_path": artifact.path,
        "artifact_brief": artifact.brief,
        "resolved_context": resolved_context,
        "plan_tree": _render_plan_tree(plan),
    }

    if artifact.type == ArtifactType.SKILL:
        template_vars["plugin_name"] = plan.plugin.name

    return template_vars


def _render_plan_tree(plan: PlanManifest) -> str:
    """Render the full artifact list so the skill generator can link to every file."""
    by_type: dict[str, list[tuple[str, str]]] = {}
    for artifact in plan.artifacts:
        by_type.setdefault(artifact.type.value, []).append(
            (artifact.path, artifact.brief)
        )

    lines: list[str] = []
    for type_name in ("skill", "reference", "command", "agent"):
        artifacts = by_type.get(type_name, [])
        if not artifacts:
            continue
        lines.append(f"**{type_name.title()}s ({len(artifacts)}):**")
        for path, brief in artifacts:
            short = brief[:120] + ("…" if len(brief) > 120 else "")
            lines.append(f"- `{path}` — {short}")
        lines.append("")

    return "\n".join(lines).rstrip()
