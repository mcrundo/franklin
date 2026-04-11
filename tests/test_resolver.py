"""Tests for the feeds_from resolver."""

from __future__ import annotations

from datetime import UTC, datetime

from franklin.reducer import resolve_feeds
from franklin.schema import (
    ActionableWorkflow,
    AntiPattern,
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterSidecar,
    Classification,
    CodeExample,
    Concept,
    CrossChapterTheme,
    DecisionRule,
    Importance,
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
        classification=Classification(
            domain="software",
            book_type="patterns_and_practices",
            audience="senior Rails developers",
            primary_intent="teach layered architecture",
            confidence=0.9,
        ),
        cross_chapter_themes=[
            CrossChapterTheme(theme="gradual extraction", chapters=["ch04", "ch07"])
        ],
        glossary={"Service Object": "A plain Ruby object for one operation"},
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
                rationale="Bounded responsibility",
                source_location=f"{chapter_id} §2",
            )
        ],
        rules=[
            Rule(
                id=f"{chapter_id}.rule.stateless",
                rule="Services are stateless",
                applies_when="designing new services",
                source_location=f"{chapter_id} §3",
            )
        ],
        anti_patterns=[
            AntiPattern(
                id=f"{chapter_id}.anti.service-god",
                name="Service as God",
                description="A service that does too much",
                smell_signals=["method > 50 lines", "more than 5 collaborators"],
                fix="Split by domain",
                code_before_ref=f"{chapter_id}.example.bad",
                code_after_ref=f"{chapter_id}.example.good",
                source_location=f"{chapter_id} §4",
            )
        ],
        code_examples=[
            CodeExample(
                id=f"{chapter_id}.example.good",
                language="ruby",
                label="Listing 1",
                code="class X\n  def call; end\nend",
                context="minimal service object",
                source_location=f"{chapter_id} §1",
            )
        ],
        decision_rules=[
            DecisionRule(
                id=f"{chapter_id}.decision.when-service",
                question="Should this be a service?",
                yes_when=["spans multiple models"],
                no_when=["simple derived attribute"],
                source_location=f"{chapter_id} §5",
            )
        ],
        actionable_workflows=[
            ActionableWorkflow(
                id=f"{chapter_id}.workflow.extract",
                name="Extract a service",
                trigger="fat controller method",
                steps=["identify", "move", "update callers"],
                source_location=f"{chapter_id} §6",
            )
        ],
    )


def test_resolves_whole_category_path() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04"), "ch07": _sidecar("ch07")}
    ctx = resolve_feeds(["ch04.anti_patterns"], book=book, sidecars=sidecars)
    assert ctx.unresolved == []
    assert "ch04" in ctx.chapter_items
    assert "anti_patterns" in ctx.chapter_items["ch04"]
    assert len(ctx.chapter_items["ch04"]["anti_patterns"]) == 1
    assert "ch07" not in ctx.chapter_items  # ch07 wasn't requested


def test_resolves_whole_chapter_path() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    ctx = resolve_feeds(["ch04"], book=book, sidecars=sidecars)
    assert ctx.unresolved == []
    assert set(ctx.chapter_items["ch04"].keys()) >= {
        "concepts",
        "principles",
        "rules",
        "anti_patterns",
        "code_examples",
        "decision_rules",
        "actionable_workflows",
    }


def test_resolves_book_metadata() -> None:
    book = _book()
    ctx = resolve_feeds(
        ["book.metadata", "book.classification", "book.cross_chapter_themes"],
        book=book,
        sidecars={},
    )
    assert ctx.unresolved == []
    assert "metadata" in ctx.book_fields
    assert "classification" in ctx.book_fields
    assert "cross_chapter_themes" in ctx.book_fields
    assert "Layered Design" in ctx.markdown
    assert "gradual extraction" in ctx.markdown


def test_unresolved_paths_collected_not_raised() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    ctx = resolve_feeds(
        [
            "ch04.concepts",  # valid
            "ch99.concepts",  # unknown chapter
            "ch04.bogus_category",  # invalid category
            "book.nonexistent",  # invalid book field
            "",  # empty path
        ],
        book=book,
        sidecars=sidecars,
    )
    assert "concepts" in ctx.chapter_items.get("ch04", {})
    assert sorted(ctx.unresolved) == [
        "",
        "book.nonexistent",
        "ch04.bogus_category",
        "ch99.concepts",
    ]


def test_markdown_contains_book_header_even_without_book_paths() -> None:
    book = _book()
    ctx = resolve_feeds(["ch04.concepts"], book=book, sidecars={"ch04": _sidecar("ch04")})
    assert ctx.markdown.startswith("# Layered Design")
    assert "Vladimir Dementyev" in ctx.markdown


def test_markdown_renders_concepts_and_anti_patterns_in_order() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    ctx = resolve_feeds(
        ["ch04.concepts", "ch04.anti_patterns"],
        book=book,
        sidecars=sidecars,
    )
    md = ctx.markdown
    assert "### Concepts (1)" in md
    assert "### Anti Patterns (1)" in md
    # Concepts header appears before anti patterns header (schema field order)
    assert md.index("### Concepts") < md.index("### Anti Patterns")
    assert "ch04.concept.service-object" in md
    assert "ch04.anti.service-god" in md
    assert "Service as God" in md
    assert "**Fix.** Split by domain" in md


def test_markdown_renders_code_examples_as_fenced_blocks() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    ctx = resolve_feeds(["ch04.code_examples"], book=book, sidecars=sidecars)
    md = ctx.markdown
    assert "#### `ch04.example.good` — Listing 1" in md
    assert "```ruby" in md
    assert "class X" in md


def test_markdown_renders_workflows_with_steps() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    ctx = resolve_feeds(["ch04.actionable_workflows"], book=book, sidecars=sidecars)
    md = ctx.markdown
    assert "**Extract a service**" in md
    assert "**Steps:**" in md
    assert "- identify" in md
    assert "- move" in md


def test_markdown_renders_decision_rules_with_conditions() -> None:
    book = _book()
    sidecars = {"ch04": _sidecar("ch04")}
    ctx = resolve_feeds(["ch04.decision_rules"], book=book, sidecars=sidecars)
    md = ctx.markdown
    assert "**Should this be a service?**" in md
    assert "**Yes when:** spans multiple models" in md
    assert "**No when:** simple derived attribute" in md
