"""Ingest tests — run against the real Layered Design EPUB fixture."""

from pathlib import Path

import pytest

from franklin.ingest import ingest_epub

FIXTURE = Path(__file__).resolve().parents[1] / (
    "Layered Design for Ruby on Rails Applications by Vladimir Dementyev.epub"
)


@pytest.fixture(scope="module")
def ingested() -> tuple:
    if not FIXTURE.exists():
        pytest.skip(f"Fixture not found: {FIXTURE}")
    return ingest_epub(FIXTURE)


def test_manifest_has_title(ingested: tuple) -> None:
    manifest, _ = ingested
    assert "Layered Design" in manifest.metadata.title


def test_has_authors(ingested: tuple) -> None:
    manifest, _ = ingested
    assert manifest.metadata.authors
    assert any("Dementyev" in a for a in manifest.metadata.authors)


def test_produces_chapters(ingested: tuple) -> None:
    _, chapters = ingested
    assert len(chapters) >= 5, f"expected several chapters, got {len(chapters)}"


def test_chapters_have_content(ingested: tuple) -> None:
    _, chapters = ingested
    for c in chapters:
        assert c.text.strip(), f"chapter {c.chapter_id} is empty"
        assert c.word_count > 0


def test_chapter_ids_monotonic(ingested: tuple) -> None:
    _, chapters = ingested
    for i, c in enumerate(chapters, start=1):
        assert c.chapter_id == f"ch{i:02d}"
        assert c.order == i


def test_detects_code_examples(ingested: tuple) -> None:
    _, chapters = ingested
    total_code = sum(len(c.code_blocks) for c in chapters)
    assert total_code > 0, "expected a technical book to contain code blocks"


def test_layered_design_classification_end_to_end(tmp_path: Path) -> None:
    """Run the full ingest CLI flow and check the classifier got Layered Design right."""
    if not FIXTURE.exists():
        pytest.skip(f"Fixture not found: {FIXTURE}")

    from franklin.checkpoint import RunDirectory
    from franklin.classify import classify_chapters
    from franklin.schema import ChapterKind

    run = RunDirectory(tmp_path / "run")
    run.ensure()
    manifest, chapters = ingest_epub(FIXTURE)
    classifications = classify_chapters(chapters)
    for entry in manifest.structure.toc:
        result = classifications[entry.id]
        entry.kind = result.kind
        entry.kind_confidence = result.confidence
        entry.kind_reason = result.reason

    by_title = {e.title: e for e in manifest.structure.toc}

    assert by_title["Table of Contents"].kind == ChapterKind.FRONT_MATTER
    assert by_title["Preface"].kind == ChapterKind.INTRODUCTION

    part_titles = [t for t in by_title if t.startswith("Part ")]
    assert len(part_titles) >= 3
    for title in part_titles:
        assert by_title[title].kind == ChapterKind.PART_DIVIDER

    chapter_titles = [t for t in by_title if t.startswith("Chapter ")]
    assert len(chapter_titles) >= 10
    for title in chapter_titles:
        assert by_title[title].kind == ChapterKind.CONTENT

    assert by_title["Index"].kind == ChapterKind.BACK_MATTER
    assert by_title["Other Books You May Enjoy"].kind == ChapterKind.BACK_MATTER

    # Roundtrip: save the classified manifest and load it back.
    run.save_book(manifest)
    reloaded = run.load_book()
    assert reloaded.structure.toc[0].kind == manifest.structure.toc[0].kind
