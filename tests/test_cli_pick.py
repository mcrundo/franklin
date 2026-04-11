"""Tests for the interactive ``franklin pick`` picker UX."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from franklin.cli import _prompt_pick_candidate, _questionary_pick
from franklin.picker import BookCandidate


def _candidate(name: str = "book", runs_base: Path | None = None) -> BookCandidate:
    path = runs_base or Path(f"/tmp/{name}.epub")
    return BookCandidate(
        path=path,
        size_bytes=12345,
        run_slug="book-slug",
        existing_run=None,
    )


def test_prompt_pick_falls_back_to_table_when_not_tty() -> None:
    """A non-TTY invocation uses the numbered prompt fallback."""
    candidates = [_candidate("a"), _candidate("b")]

    with (
        patch("sys.stdin.isatty", return_value=False),
        patch("sys.stdout.isatty", return_value=False),
        patch("franklin.cli._fallback_numbered_pick") as fallback,
        patch("franklin.cli._questionary_pick") as questionary_branch,
    ):
        fallback.return_value = candidates[1]
        result = _prompt_pick_candidate(candidates)

    fallback.assert_called_once_with(candidates)
    questionary_branch.assert_not_called()
    assert result is candidates[1]


def test_prompt_pick_uses_questionary_when_tty() -> None:
    """An interactive TTY goes through the questionary arrow-key branch."""
    candidates = [_candidate("a"), _candidate("b")]

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("franklin.cli._questionary_pick") as questionary_branch,
        patch("franklin.cli._fallback_numbered_pick") as fallback,
    ):
        questionary_branch.return_value = candidates[0]
        result = _prompt_pick_candidate(candidates)

    questionary_branch.assert_called_once_with(candidates)
    fallback.assert_not_called()
    assert result is candidates[0]


def test_questionary_pick_returns_selected_candidate() -> None:
    """questionary.select returns the Choice value, which is the candidate."""
    candidates = [_candidate("a"), _candidate("b")]

    fake_select = MagicMock()
    fake_select.ask.return_value = candidates[1]

    with patch("questionary.select", return_value=fake_select) as sel:
        result = _questionary_pick(candidates)

    assert result is candidates[1]
    # Confirm we asked with arrow-key friendly config
    _, kwargs = sel.call_args
    assert kwargs["use_search_filter"] is True


def test_questionary_pick_returns_none_on_cancel() -> None:
    """Hitting Ctrl-C or picking '(cancel)' returns None."""
    candidates = [_candidate("a")]

    fake_select = MagicMock()
    fake_select.ask.return_value = None

    with patch("questionary.select", return_value=fake_select):
        result = _questionary_pick(candidates)

    assert result is None
