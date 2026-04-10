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
