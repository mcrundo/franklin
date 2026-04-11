"""Tests for the book discovery picker."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from franklin.checkpoint import RunDirectory
from franklin.picker import discover_books
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
)


def _seed_run(runs_base: Path, slug: str) -> None:
    run = RunDirectory(runs_base / slug)
    run.ensure()
    run.save_book(
        BookManifest(
            franklin_version="0.1.0",
            source=BookSource(
                path=f"/books/{slug}.epub",
                sha256="0" * 64,
                format="epub",
                ingested_at=datetime.now(UTC),
            ),
            metadata=BookMetadata(title="Seeded", authors=["Ada"]),
            structure=BookStructure(),
        )
    )


def test_discover_books_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    assert discover_books(tmp_path / "nope", runs_base=tmp_path / "runs") == []


def test_discover_books_finds_epub_and_pdf(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "alpha.epub").write_bytes(b"fake")
    (library / "beta.pdf").write_bytes(b"fake")
    (library / "unrelated.txt").write_bytes(b"fake")

    results = discover_books(library, runs_base=tmp_path / "runs")
    names = sorted(c.path.name for c in results)
    assert names == ["alpha.epub", "beta.pdf"]


def test_discover_books_recursive_finds_nested(tmp_path: Path) -> None:
    library = tmp_path / "books"
    (library / "sub").mkdir(parents=True)
    (library / "sub/nested.epub").write_bytes(b"fake")

    results = discover_books(library, runs_base=tmp_path / "runs")
    assert len(results) == 1
    assert results[0].path.name == "nested.epub"


def test_discover_books_non_recursive_skips_subdirs(tmp_path: Path) -> None:
    library = tmp_path / "books"
    (library / "sub").mkdir(parents=True)
    (library / "sub/nested.epub").write_bytes(b"fake")
    (library / "top.epub").write_bytes(b"fake")

    results = discover_books(library, runs_base=tmp_path / "runs", recursive=False)
    names = [c.path.name for c in results]
    assert names == ["top.epub"]


def test_discover_books_skips_hidden_files(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / ".hidden.epub").write_bytes(b"fake")
    (library / "visible.epub").write_bytes(b"fake")

    results = discover_books(library, runs_base=tmp_path / "runs")
    assert [c.path.name for c in results] == ["visible.epub"]


def test_discover_books_annotates_existing_run(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "Known Book.epub").write_bytes(b"fake")
    runs_base = tmp_path / "runs"
    _seed_run(runs_base, "known-book")

    results = discover_books(library, runs_base=runs_base)
    assert len(results) == 1
    candidate = results[0]
    assert candidate.is_processed is True
    assert candidate.existing_run is not None
    assert candidate.existing_run.slug == "known-book"


def test_discover_books_marks_new_files(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "fresh.epub").write_bytes(b"fake")

    results = discover_books(library, runs_base=tmp_path / "runs")
    assert results[0].is_processed is False
    assert results[0].existing_run is None


def test_discover_books_respects_max_results(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    for i in range(20):
        (library / f"b{i:02d}.epub").write_bytes(b"fake")

    results = discover_books(library, runs_base=tmp_path / "runs", max_results=5)
    assert len(results) == 5
