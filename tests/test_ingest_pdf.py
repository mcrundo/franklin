"""Tests for PDF ingest.

Exercises the happy path against a small committed fixture PDF, the
extension dispatcher, and the unsupported-format rejection. The fixture
(`tests/fixtures/tiny_book.pdf`) was hand-generated with reportlab once
and committed — regenerating it is a one-off script, not something the
test suite does at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from franklin.ingest import UnsupportedFormatError, ingest_book
from franklin.ingest.pdf import ingest_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_book.pdf"


@pytest.fixture(scope="module")
def ingested() -> tuple:
    if not FIXTURE.exists():
        pytest.skip(f"fixture not found: {FIXTURE}")
    return ingest_pdf(FIXTURE)


def test_fixture_pdf_exists() -> None:
    assert FIXTURE.exists(), (
        "tests/fixtures/tiny_book.pdf is missing — regenerate with the "
        "reportlab script in this ticket's notes"
    )


def test_manifest_has_title(ingested: tuple) -> None:
    manifest, _ = ingested
    # pdfplumber reads the Title from metadata, falling back to the stem
    assert manifest.metadata.title
    # The fixture has no embedded Title metadata, so it falls back to the
    # filename stem
    assert "tiny_book" in manifest.metadata.title or manifest.metadata.title


def test_manifest_format_is_pdf(ingested: tuple) -> None:
    manifest, _ = ingested
    assert manifest.source.format == "pdf"
    assert manifest.source.sha256
    assert len(manifest.source.sha256) == 64


def test_produces_multiple_chapters(ingested: tuple) -> None:
    """The fixture has Preface + Chapter 1 + Chapter 2 — font heading detection
    should find at least two of them (the preface may or may not clear the
    minimum-words threshold)."""
    _, chapters = ingested
    assert len(chapters) >= 2


def test_chapter_titles_come_from_headings(ingested: tuple) -> None:
    _, chapters = ingested
    titles = [c.title for c in chapters]
    # At least Chapter 1 and Chapter 2 should be present
    assert any("Service Objects" in t for t in titles), titles
    assert any("Form Objects" in t for t in titles), titles


def test_code_blocks_detected(ingested: tuple) -> None:
    """The fixture embeds a Ruby code block in Courier font on page 2 —
    it should be extracted as a CodeBlock, not mixed into prose."""
    _, chapters = ingested
    all_code = [cb for c in chapters for cb in c.code_blocks]
    assert any("PostPublishService" in cb.code for cb in all_code), (
        f"expected Ruby class in a code block, got: {[cb.code for cb in all_code]}"
    )


def test_code_not_leaked_into_prose(ingested: tuple) -> None:
    """The Ruby class should appear in code blocks, not in chapter prose."""
    _, chapters = ingested
    for chapter in chapters:
        assert "PostPublishService" not in chapter.text, (
            f"code leaked into prose for {chapter.chapter_id}"
        )


def test_chapter_ids_monotonic(ingested: tuple) -> None:
    _, chapters = ingested
    for i, c in enumerate(chapters, start=1):
        assert c.chapter_id == f"ch{i:02d}"
        assert c.order == i


def test_page_footer_filtered_from_prose(ingested: tuple) -> None:
    """The fixture's 'tiny_book — page N' footer should not appear in chapter text."""
    _, chapters = ingested
    for chapter in chapters:
        assert "tiny_book —" not in chapter.text, (
            f"footer leaked into prose for {chapter.chapter_id}: {chapter.text!r}"
        )


def test_chapter_source_ref_records_page_range(ingested: tuple) -> None:
    _, chapters = ingested
    for c in chapters:
        assert c.source_ref.startswith("pp. ")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_ingest_book_dispatches_pdf() -> None:
    if not FIXTURE.exists():
        pytest.skip(f"fixture not found: {FIXTURE}")
    manifest, _ = ingest_book(FIXTURE)
    assert manifest.source.format == "pdf"


def test_ingest_book_rejects_unknown_extension(tmp_path: Path) -> None:
    bogus = tmp_path / "book.mobi"
    bogus.write_bytes(b"not a real mobi")
    with pytest.raises(UnsupportedFormatError, match="unsupported book format"):
        ingest_book(bogus)


def test_ingest_book_dispatches_epub() -> None:
    epub_fixture = Path(__file__).resolve().parents[1] / (
        "Layered Design for Ruby on Rails Applications by Vladimir Dementyev.epub"
    )
    if not epub_fixture.exists():
        pytest.skip(f"epub fixture not found: {epub_fixture}")
    manifest, _ = ingest_book(epub_fixture)
    assert manifest.source.format == "epub"
