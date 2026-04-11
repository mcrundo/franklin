"""Tests for the interactive book metadata confirmation (RUB-84 sibling)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from franklin.cli import _maybe_confirm_metadata
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
)


def _manifest(title: str = "Wrong Title", authors: list[str] | None = None) -> BookManifest:
    return BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path="/books/x.epub",
            sha256="0" * 64,
            format="epub",
            ingested_at=datetime.now(UTC),
        ),
        metadata=BookMetadata(title=title, authors=authors or ["Old Author"]),
        structure=BookStructure(),
    )


def test_skip_true_never_prompts() -> None:
    m = _manifest()
    with patch("typer.confirm") as confirm:
        _maybe_confirm_metadata(m, skip=True)
    confirm.assert_not_called()
    assert m.metadata.title == "Wrong Title"


def test_non_tty_skips_prompt() -> None:
    m = _manifest()
    with patch("sys.stdin.isatty", return_value=False), patch("typer.confirm") as confirm:
        _maybe_confirm_metadata(m, skip=False)
    confirm.assert_not_called()


def test_confirm_yes_keeps_metadata() -> None:
    m = _manifest()
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("typer.confirm", return_value=True),
    ):
        _maybe_confirm_metadata(m, skip=False)
    assert m.metadata.title == "Wrong Title"
    assert m.metadata.authors == ["Old Author"]


def test_confirm_no_edits_metadata() -> None:
    m = _manifest()
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("typer.confirm", return_value=False),
        patch("typer.prompt", side_effect=["Right Title", "Alice, Bob"]),
    ):
        _maybe_confirm_metadata(m, skip=False)
    assert m.metadata.title == "Right Title"
    assert m.metadata.authors == ["Alice", "Bob"]


def test_edit_blank_keeps_original_title() -> None:
    m = _manifest()
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("typer.confirm", return_value=False),
        patch("typer.prompt", side_effect=["   ", "Alice"]),
    ):
        _maybe_confirm_metadata(m, skip=False)
    assert m.metadata.title == "Wrong Title"
    assert m.metadata.authors == ["Alice"]


def test_edit_unknown_authors_leaves_empty() -> None:
    m = _manifest(authors=[])
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("typer.confirm", return_value=False),
        patch("typer.prompt", side_effect=["Right Title", "(unknown)"]),
    ):
        _maybe_confirm_metadata(m, skip=False)
    assert m.metadata.authors == []
