"""Tests for the reduce-stage artifact generators."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from franklin.reducer.generators import (
    CACHE_BREAKPOINT,
    _build_template_vars,
    _render_plan_tree,
    _template_name_for,
    generate_artifact,
)
from franklin.schema import (
    ActionableWorkflow,
    AntiPattern,
    Artifact,
    ArtifactType,
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterSidecar,
    Concept,
    Importance,
    PlanManifest,
    PluginMeta,
    Principle,
    Rule,
)


def _book() -> BookManifest:
    return BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path="x.epub", sha256="0" * 64, format="epub", ingested_at=datetime.now(UTC)
        ),
        metadata=BookMetadata(title="Layered Design", authors=["Vladimir Dementyev"]),
        structure=BookStructure(),
    )


def _sidecar(chapter_id: str) -> ChapterSidecar:
    return ChapterSidecar(
        chapter_id=chapter_id,
        title=f"Chapter {chapter_id}",
        order=int(chapter_id.removeprefix("ch")),
        source_ref=f"OEBPS/{chapter_id}.xhtml",
        word_count=3000,
        summary=f"Summary for {chapter_id}.",
        concepts=[
            Concept(
                id=f"{chapter_id}.concept.service-object",
                name="Service Object",
                definition="A plain Ruby object for one operation",
                importance=Importance.HIGH,
                source_location=f"{chapter_id} §1",
            )
        ],
        principles=[
            Principle(
                id=f"{chapter_id}.principle.single-responsibility",
                statement="Do one thing",
                source_location=f"{chapter_id} §2",
            )
        ],
        rules=[
            Rule(
                id=f"{chapter_id}.rule.stateless",
                rule="Services are stateless",
                source_location=f"{chapter_id} §3",
            )
        ],
        anti_patterns=[
            AntiPattern(
                id=f"{chapter_id}.anti.fat-model",
                name="Fat Model",
                description="Model with too much logic",
                fix="Extract to services",
                source_location=f"{chapter_id} §4",
            )
        ],
        actionable_workflows=[
            ActionableWorkflow(
                id=f"{chapter_id}.workflow.extract",
                name="Extract a service",
                steps=["find", "move", "delete"],
                source_location=f"{chapter_id} §5",
            )
        ],
    )


def _plan(artifacts: list[Artifact]) -> PlanManifest:
    return PlanManifest(
        book_id="layered-design",
        generated_at=datetime.now(UTC),
        planner_model="claude-opus-4-6",
        planner_rationale="test rationale",
        plugin=PluginMeta(name="layered-rails", description="Test plugin"),
        artifacts=artifacts,
        coherence_rules=["Use service object consistently", "Quote code verbatim"],
    )


def _reference_artifact() -> Artifact:
    return Artifact(
        id="art.ref.service-objects",
        type=ArtifactType.REFERENCE,
        path="skills/layered-rails/references/patterns/service-objects.md",
        brief="Pattern reference for service objects.",
        feeds_from=["ch04.concepts", "ch04.principles", "ch04.rules"],
        estimated_output_tokens=2500,
    )


def _skill_artifact() -> Artifact:
    return Artifact(
        id="art.skill.root",
        type=ArtifactType.SKILL,
        path="skills/layered-rails/SKILL.md",
        brief="Router skill.",
        feeds_from=["book.metadata"],
        estimated_output_tokens=3000,
    )


class _FakeStream:
    def __init__(self, response: Any) -> None:
        self._response = response

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._response


class _FakeClient:
    def __init__(self, content: str) -> None:
        self._content = content
        self.messages = self
        self.last_kwargs: dict[str, Any] | None = None

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.last_kwargs = kwargs
        return _FakeStream(
            SimpleNamespace(
                content=[
                    SimpleNamespace(type="tool_use", input={"content": self._content})
                ],
                stop_reason="tool_use",
                usage=SimpleNamespace(
                    input_tokens=1000,
                    output_tokens=500,
                    cache_read_input_tokens=800,
                    cache_creation_input_tokens=200,
                ),
            )
        )


def test_template_name_dispatch() -> None:
    assert _template_name_for(ArtifactType.SKILL) == "generate_skill"
    assert _template_name_for(ArtifactType.REFERENCE) == "generate_reference"
    assert _template_name_for(ArtifactType.COMMAND) == "generate_command"
    assert _template_name_for(ArtifactType.AGENT) == "generate_agent"


def test_build_template_vars_includes_coherence_and_context() -> None:
    from franklin.reducer.resolver import resolve_feeds

    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    plan = _plan([_reference_artifact()])
    ctx = resolve_feeds(["ch04.concepts"], book=book, sidecars=sidecars)

    vars = _build_template_vars(artifact=_reference_artifact(), plan=plan, context=ctx)
    assert "Use service object consistently" in vars["coherence_rules"]
    assert "Layered Design" in vars["book_context"]
    assert "ch04.concept.service-object" in vars["resolved_context"]
    assert vars["artifact_path"].endswith("service-objects.md")
    # plan_tree is now included for every artifact type so generators can
    # produce correct relative links. plugin_name remains skill-only.
    assert "plan_tree" in vars
    assert "service-objects.md" in vars["plan_tree"]
    assert "plugin_name" not in vars


def test_build_template_vars_adds_plugin_tree_for_skill() -> None:
    from franklin.reducer.resolver import resolve_feeds

    book = _book()
    plan = _plan([_skill_artifact(), _reference_artifact()])
    ctx = resolve_feeds(["book.metadata"], book=book, sidecars={})

    vars = _build_template_vars(artifact=_skill_artifact(), plan=plan, context=ctx)
    assert vars["plugin_name"] == "layered-rails"
    assert "References (1)" in vars["plan_tree"]
    assert "service-objects.md" in vars["plan_tree"]


def test_render_plan_tree_groups_by_type() -> None:
    plan = _plan(
        [
            _skill_artifact(),
            _reference_artifact(),
            Artifact(
                id="art.cmd.spec",
                type=ArtifactType.COMMAND,
                path="commands/spec-test.md",
                brief="Apply the specification test.",
                feeds_from=["ch08.actionable_workflows"],
            ),
            Artifact(
                id="art.agent.reviewer",
                type=ArtifactType.AGENT,
                path="agents/layered-rails-reviewer.md",
                brief="Reviewer agent.",
                feeds_from=["ch07.anti_patterns"],
            ),
        ]
    )
    tree = _render_plan_tree(plan)
    assert "**Skills (1):**" in tree
    assert "**References (1):**" in tree
    assert "**Commands (1):**" in tree
    assert "**Agents (1):**" in tree


def test_generate_artifact_splits_on_cache_breakpoint() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    plan = _plan([_reference_artifact()])
    client = _FakeClient("# Service Objects\n\nGenerated body.")

    result = generate_artifact(
        _reference_artifact(),
        plan=plan,
        book=book,
        sidecars=sidecars,
        client=client,
    )

    assert result.content.startswith("# Service Objects")
    assert result.cache_read_tokens == 800
    assert result.cache_creation_tokens == 200

    assert client.last_kwargs is not None
    user_content = client.last_kwargs["messages"][0]["content"]
    assert isinstance(user_content, list)
    assert len(user_content) == 2
    assert user_content[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in user_content[1]
    # Cache breakpoint marker itself should not appear in either block
    assert CACHE_BREAKPOINT not in user_content[0]["text"]
    assert CACHE_BREAKPOINT not in user_content[1]["text"]
    # The stable prefix contains the coherence rules, the tail contains the brief
    assert "Use service object consistently" in user_content[0]["text"]
    assert "service-objects.md" in user_content[1]["text"]


def test_generate_artifact_rejects_empty_content() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    plan = _plan([_reference_artifact()])
    client = _FakeClient("")

    with pytest.raises(RuntimeError, match="empty or non-string"):
        generate_artifact(
            _reference_artifact(),
            plan=plan,
            book=book,
            sidecars=sidecars,
            client=client,
        )


def test_generate_artifact_uses_system_cache() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    plan = _plan([_reference_artifact()])
    client = _FakeClient("test content")

    generate_artifact(
        _reference_artifact(),
        plan=plan,
        book=book,
        sidecars=sidecars,
        client=client,
    )

    assert client.last_kwargs is not None
    system_arg = client.last_kwargs["system"]
    assert isinstance(system_arg, list)
    assert system_arg[0]["cache_control"] == {"type": "ephemeral"}
