"""Schema roundtrip tests — ensures pydantic models serialize and parse cleanly."""

from datetime import UTC, datetime

from franklin.schema import (
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
    dump_json,
    parse_json,
)


def _sample_book() -> BookManifest:
    return BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path="x.epub",
            sha256="0" * 64,
            format="epub",
            ingested_at=datetime.now(UTC),
        ),
        metadata=BookMetadata(title="Test Book", authors=["Ada"]),
        structure=BookStructure(),
    )


def test_book_manifest_roundtrip() -> None:
    book = _sample_book()
    restored = parse_json(BookManifest, dump_json(book))
    assert restored.metadata.title == "Test Book"
    assert restored.metadata.authors == ["Ada"]


def test_chapter_sidecar_roundtrip() -> None:
    sidecar = ChapterSidecar(
        chapter_id="ch01",
        title="Intro",
        order=1,
        source_ref="OEBPS/ch01.xhtml",
        word_count=100,
        summary="A short chapter",
        concepts=[
            Concept(
                id="ch01.concept.x",
                name="X",
                definition="a thing",
                importance=Importance.HIGH,
                source_location="ch01 §1",
            )
        ],
    )
    restored = parse_json(ChapterSidecar, dump_json(sidecar))
    assert restored.concepts[0].name == "X"
    assert restored.concepts[0].importance == Importance.HIGH


def test_plan_manifest_roundtrip() -> None:
    plan = PlanManifest(
        book_id="test",
        generated_at=datetime.now(UTC),
        planner_model="claude-opus-4-6",
        planner_rationale="test",
        plugin=PluginMeta(name="test", description="test"),
        artifacts=[
            Artifact(
                id="art.x",
                type=ArtifactType.SKILL,
                path="skills/x/SKILL.md",
                brief="test",
                feeds_from=["ch01.concepts"],
            )
        ],
    )
    restored = parse_json(PlanManifest, dump_json(plan))
    assert restored.artifacts[0].type == ArtifactType.SKILL
