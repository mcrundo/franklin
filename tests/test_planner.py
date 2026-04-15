"""Tests for the plan-stage designer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from _fakes import FakeClient
from franklin.planner import (
    build_distilled_view,
    build_tool_schema,
    build_user_prompt,
    design_plan,
)
from franklin.schema import (
    ActionableWorkflow,
    AntiPattern,
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterSidecar,
    CodeExample,
    Concept,
    DecisionRule,
    Importance,
    PlanManifest,
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
                definition="A plain object for one operation",
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
                id=f"{chapter_id}.anti.service-god",
                name="Service as God",
                description="A service that does too much",
                fix="Split by domain",
                source_location=f"{chapter_id} §4",
            )
        ],
        decision_rules=[
            DecisionRule(
                id=f"{chapter_id}.decision.when-service",
                question="Should this be a service?",
                source_location=f"{chapter_id} §5",
            )
        ],
        actionable_workflows=[
            ActionableWorkflow(
                id=f"{chapter_id}.workflow.extract",
                name="Extract a service",
                steps=["find", "extract", "delete"],
                source_location=f"{chapter_id} §6",
            )
        ],
        code_examples=[
            CodeExample(
                id=f"{chapter_id}.example.good",
                language="ruby",
                label="Listing 1",
                code="class X; end",
                source_location=f"{chapter_id} §1",
            )
        ],
    )


def test_distilled_view_includes_every_chapter_and_category() -> None:
    book = _book()
    sidecars = [_sidecar("ch04"), _sidecar("ch05")]
    view = build_distilled_view(book, sidecars)

    assert "Layered Design" in view
    assert "Vladimir Dementyev" in view
    for cid in ("ch04", "ch05"):
        assert f"## {cid}:" in view
        assert f"{cid}.concept.service-object" in view
        assert f"{cid}.principle.single-responsibility" in view
        assert f"{cid}.rule.stateless" in view
        assert f"{cid}.anti.service-god" in view
        assert f"{cid}.decision.when-service" in view
        assert f"{cid}.workflow.extract" in view


def test_distilled_view_omits_full_code_bodies() -> None:
    """Code example bodies bloat the prompt; only labels should appear."""
    sidecar = _sidecar("ch04")
    view = build_distilled_view(_book(), [sidecar])
    assert "Listing 1" in view
    assert "class X; end" not in view


def test_build_user_prompt_substitutes_placeholder() -> None:
    prompt = build_user_prompt(_book(), [_sidecar("ch04")])
    assert "{{distilled_book}}" not in prompt
    assert "Layered Design" in prompt
    assert "save_plan_proposal" in prompt


def test_build_tool_schema_is_object_with_required_categories() -> None:
    schema = build_tool_schema()
    assert schema["type"] == "object"
    for field in ("plugin", "artifacts", "coherence_rules", "planner_rationale"):
        assert field in schema["properties"]


_PLANNER_USAGE = {"input_tokens": 50_000, "output_tokens": 8_000}


def _client(payload: dict[str, Any]) -> FakeClient:
    return FakeClient(payload, usage=_PLANNER_USAGE)


def test_design_plan_merges_with_run_metadata() -> None:
    book = _book()
    sidecars = [_sidecar("ch04")]
    payload = {
        "plugin": {
            "name": "layered-rails",
            "version": "0.1.0",
            "description": "Layered design for Rails apps",
            "keywords": ["rails", "architecture"],
        },
        "planner_rationale": "Patterns-heavy book; proposing a skill plus reference tree.",
        "artifacts": [
            {
                "id": "art.skill.root",
                "type": "skill",
                "path": "skills/layered-rails/SKILL.md",
                "brief": "Router",
                "feeds_from": ["book.metadata", "ch04.concepts"],
                "estimated_output_tokens": 3000,
            }
        ],
        "coherence_rules": ["Use 'service object' consistently"],
        "skipped_artifact_types": [
            {"type": "mcp_server", "reason": "no API content"},
        ],
        "estimated_total_output_tokens": 3000,
        "estimated_reduce_calls": 1,
    }
    client = _client(payload)

    plan, in_toks, out_toks = design_plan(book, sidecars, client=client)

    assert isinstance(plan, PlanManifest)
    assert plan.book_id == "layered-design"
    assert plan.planner_model.startswith("claude-opus")
    assert plan.plugin.name == "layered-rails"
    assert plan.artifacts[0].path == "skills/layered-rails/SKILL.md"
    assert plan.skipped_artifact_types[0].type == "mcp_server"
    assert in_toks == 50_000
    assert out_toks == 8_000

    assert client.last_kwargs is not None
    assert client.last_kwargs["tool_choice"]["name"] == "save_plan_proposal"


def test_design_plan_rejects_invalid_proposal() -> None:
    bad = {"plugin": {"name": "x"}, "planner_rationale": "test"}  # missing plugin.description
    client = _client(bad)
    with pytest.raises(RuntimeError, match="invalid proposal"):
        design_plan(_book(), [_sidecar("ch04")], client=client)


def test_design_plan_recovers_from_stray_extra_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same recovery as the mapper: a single stray field on a sub-object
    shouldn't kill an entire plan call. The plan stage is one big LLM
    call so this matters more than the per-chapter map case."""
    payload = {
        "plugin": {
            "name": "layered-rails",
            "version": "0.1.0",
            "description": "Layered design for Rails apps",
            "keywords": ["rails"],
        },
        "planner_rationale": "Test recovery from stray field.",
        "artifacts": [
            {
                "id": "art.skill.root",
                "type": "skill",
                "path": "skills/layered-rails/SKILL.md",
                "brief": "Router",
                "feeds_from": ["book.metadata", "ch04.concepts"],
                "estimated_output_tokens": 3000,
                "rogue_field": "the LLM made this up",
            }
        ],
        "coherence_rules": [],
        "skipped_artifact_types": [],
        "estimated_total_output_tokens": 3000,
        "estimated_reduce_calls": 1,
    }
    client = _client(payload)

    with caplog.at_level("WARNING", logger="franklin.llm.validation"):
        plan, _, _ = design_plan(_book(), [_sidecar("ch04")], client=client)

    assert plan.artifacts[0].id == "art.skill.root"
    assert any("rogue_field" in r.message for r in caplog.records)
