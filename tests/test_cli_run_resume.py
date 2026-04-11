"""Tests for the resume-detection prompt on ``franklin run``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from franklin.checkpoint import RunDirectory
from franklin.cli import run_pipeline
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    NormalizedChapter,
)


@pytest.fixture
def book_epub(tmp_path: Path) -> Path:
    path = tmp_path / "book.epub"
    path.write_bytes(b"not a real epub")
    return path


def _seed_ingest(run_dir: Path, title: str = "Seeded") -> None:
    rd = RunDirectory(run_dir)
    rd.ensure()
    rd.save_book(
        BookManifest(
            franklin_version="0.1.0",
            source=BookSource(
                path="x.epub",
                sha256="0" * 64,
                format="epub",
                ingested_at=datetime.now(UTC),
            ),
            metadata=BookMetadata(title=title, authors=["Ada"]),
            structure=BookStructure(),
        )
    )
    rd.save_raw_chapter(
        NormalizedChapter(
            chapter_id="ch01",
            title="Ch1",
            order=1,
            source_ref="pp 1",
            word_count=3,
            text="one two three",
        )
    )


def _patch_all_stages() -> tuple[Any, dict[str, MagicMock]]:
    from contextlib import ExitStack

    mocks = {
        "ingest": MagicMock(),
        "map": MagicMock(),
        "plan": MagicMock(),
        "reduce": MagicMock(),
        "assemble": MagicMock(),
    }
    stack = ExitStack()
    stack.enter_context(patch("franklin.cli.ingest", mocks["ingest"]))
    stack.enter_context(patch("franklin.cli.map_chapters", mocks["map"]))
    stack.enter_context(patch("franklin.cli.plan_pipeline", mocks["plan"]))
    stack.enter_context(patch("franklin.cli.reduce_pipeline", mocks["reduce"]))
    stack.enter_context(patch("franklin.cli.assemble_pipeline", mocks["assemble"]))
    return stack, mocks


from typing import Any  # noqa: E402


def test_resume_detection_skipped_when_run_dir_missing(book_epub: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "fresh"
    stack, mocks = _patch_all_stages()
    with stack, patch("typer.confirm") as confirm:
        run_pipeline(
            book_path=book_epub,
            output=run_dir,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=False,
            repo=None,
            branch="main",
            create_pr=False,
            public=False,
        )
    confirm.assert_not_called()
    assert mocks["ingest"].called


def test_resume_with_yes_flag_auto_confirms(book_epub: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "partial"
    _seed_ingest(run_dir)
    stack, mocks = _patch_all_stages()
    with stack, patch("typer.confirm") as confirm:
        run_pipeline(
            book_path=book_epub,
            output=run_dir,
            force=False,
            yes=True,
            estimate=False,
            clean=False,
            push=False,
            repo=None,
            branch="main",
            create_pr=False,
            public=False,
        )
    confirm.assert_not_called()
    assert mocks["assemble"].called


def test_resume_prompt_accepted_continues(book_epub: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "partial"
    _seed_ingest(run_dir)
    stack, mocks = _patch_all_stages()
    with stack, patch("typer.confirm", return_value=True) as confirm:
        run_pipeline(
            book_path=book_epub,
            output=run_dir,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=False,
            repo=None,
            branch="main",
            create_pr=False,
            public=False,
        )
    confirm.assert_called_once()
    assert mocks["assemble"].called


def test_resume_prompt_declined_aborts(book_epub: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "partial"
    _seed_ingest(run_dir)
    stack, mocks = _patch_all_stages()
    with stack, patch("typer.confirm", return_value=False), pytest.raises(typer.Exit) as exc:
        run_pipeline(
            book_path=book_epub,
            output=run_dir,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=False,
            repo=None,
            branch="main",
            create_pr=False,
            public=False,
        )
    assert exc.value.exit_code == 0
    assert not mocks["map"].called
    assert not mocks["assemble"].called


def test_force_flag_skips_resume_prompt(book_epub: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "partial"
    _seed_ingest(run_dir)
    stack, mocks = _patch_all_stages()
    with stack, patch("typer.confirm") as confirm:
        run_pipeline(
            book_path=book_epub,
            output=run_dir,
            force=True,
            yes=False,
            estimate=False,
            clean=False,
            push=False,
            repo=None,
            branch="main",
            create_pr=False,
            public=False,
        )
    confirm.assert_not_called()
    assert mocks["ingest"].called
