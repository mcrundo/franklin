"""Tests for the book discovery picker."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from franklin.checkpoint import RunDirectory
from franklin.picker import (
    ALL_FORMATS,
    DEFAULT_FORMATS,
    default_search_dirs,
    discover_books,
)
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


# ---------------------------------------------------------------------------
# discover_books — core behavior
# ---------------------------------------------------------------------------


def test_discover_books_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    assert discover_books([tmp_path / "nope"], runs_base=tmp_path / "runs") == []


def test_discover_books_defaults_to_epub_only(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "alpha.epub").write_bytes(b"fake")
    (library / "beta.pdf").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs")
    names = sorted(c.path.name for c in results)
    assert names == ["alpha.epub"]


def test_discover_books_pdf_opt_in(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "alpha.epub").write_bytes(b"fake")
    (library / "beta.pdf").write_bytes(b"fake")
    (library / "unrelated.txt").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs", formats=ALL_FORMATS)
    names = sorted(c.path.name for c in results)
    assert names == ["alpha.epub", "beta.pdf"]


def test_discover_books_multiple_dirs(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "one.epub").write_bytes(b"fake")
    (dir_b / "two.epub").write_bytes(b"fake")

    results = discover_books([dir_a, dir_b], runs_base=tmp_path / "runs")
    names = sorted(c.path.name for c in results)
    assert names == ["one.epub", "two.epub"]


def test_discover_books_dedupes_across_dirs(tmp_path: Path) -> None:
    """A file reachable from two search roots must only appear once."""
    parent = tmp_path / "library"
    child = parent / "rails"
    child.mkdir(parents=True)
    (child / "book.epub").write_bytes(b"fake")

    # Pass both parent and child as search dirs; recursive=True would
    # normally find the same file twice.
    results = discover_books([parent, child], runs_base=tmp_path / "runs")
    assert len(results) == 1


def test_discover_books_recursive_finds_nested(tmp_path: Path) -> None:
    library = tmp_path / "books"
    (library / "sub").mkdir(parents=True)
    (library / "sub/nested.epub").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs")
    assert len(results) == 1
    assert results[0].path.name == "nested.epub"


def test_discover_books_non_recursive_skips_subdirs(tmp_path: Path) -> None:
    library = tmp_path / "books"
    (library / "sub").mkdir(parents=True)
    (library / "sub/nested.epub").write_bytes(b"fake")
    (library / "top.epub").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs", recursive=False)
    names = [c.path.name for c in results]
    assert names == ["top.epub"]


def test_discover_books_skips_hidden_files(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / ".hidden.epub").write_bytes(b"fake")
    (library / "visible.epub").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs")
    assert [c.path.name for c in results] == ["visible.epub"]


def test_discover_books_annotates_existing_run(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "Known Book.epub").write_bytes(b"fake")
    runs_base = tmp_path / "runs"
    _seed_run(runs_base, "known-book")

    results = discover_books([library], runs_base=runs_base)
    assert len(results) == 1
    candidate = results[0]
    assert candidate.is_processed is True
    assert candidate.existing_run is not None
    assert candidate.existing_run.slug == "known-book"


def test_discover_books_marks_new_files(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "fresh.epub").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs")
    assert results[0].is_processed is False
    assert results[0].existing_run is None


def test_discover_books_respects_max_results(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    for i in range(20):
        (library / f"b{i:02d}.epub").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs", max_results=5)
    assert len(results) == 5


# ---------------------------------------------------------------------------
# discover_books — search filter
# ---------------------------------------------------------------------------


def test_discover_books_substring_search_matches(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "Rails Antipatterns.epub").write_bytes(b"fake")
    (library / "Refactoring.epub").write_bytes(b"fake")
    (library / "Layered Design in Rails.epub").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs", query="rails")
    names = sorted(c.path.name for c in results)
    assert names == ["Layered Design in Rails.epub", "Rails Antipatterns.epub"]


def test_discover_books_search_is_case_insensitive(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "RAILS.epub").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs", query="rails")
    assert len(results) == 1


def test_discover_books_search_no_match_returns_empty(tmp_path: Path) -> None:
    library = tmp_path / "books"
    library.mkdir()
    (library / "one.epub").write_bytes(b"fake")

    results = discover_books([library], runs_base=tmp_path / "runs", query="zzz")
    assert results == []


# ---------------------------------------------------------------------------
# default_search_dirs
# ---------------------------------------------------------------------------


def test_default_search_dirs_prefers_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    custom = tmp_path / "my-books"
    custom.mkdir()
    monkeypatch.setenv("FRANKLIN_BOOKS_DIR", str(custom))

    dirs = default_search_dirs()
    assert dirs == [custom]


def test_default_search_dirs_env_var_supports_multiple(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    import os

    monkeypatch.setenv("FRANKLIN_BOOKS_DIR", f"{a}{os.pathsep}{b}")

    dirs = default_search_dirs()
    assert dirs == [a, b]


def test_default_search_dirs_falls_back_to_home_folders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / "Books").mkdir(parents=True)
    (fake_home / "Media").mkdir()
    (fake_home / "Downloads").mkdir()
    # intentionally no Documents — should be dropped
    monkeypatch.delenv("FRANKLIN_BOOKS_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    dirs = default_search_dirs()
    assert dirs == [
        fake_home / "Books",
        fake_home / "Media",
        fake_home / "Downloads",
    ]


def test_default_formats_is_epub_only() -> None:
    assert DEFAULT_FORMATS == (".epub",)


def test_all_formats_includes_pdf() -> None:
    assert ".epub" in ALL_FORMATS
    assert ".pdf" in ALL_FORMATS
