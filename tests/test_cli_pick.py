"""Tests for the interactive ``franklin pick`` picker UX."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from franklin.checkpoint import RunDirectory
from franklin.commands.pick import _prompt_pick_candidate, _questionary_pick
from franklin.picker import BookCandidate
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    ChapterKind,
    NormalizedChapter,
    TocEntry,
)
from franklin.services import MapInput, MapService


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
        patch("franklin.commands.pick._fallback_numbered_pick") as fallback,
        patch("franklin.commands.pick._questionary_pick") as questionary_branch,
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
        patch("franklin.commands.pick._questionary_pick") as questionary_branch,
        patch("franklin.commands.pick._fallback_numbered_pick") as fallback,
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


# ---------------------------------------------------------------------------
# Gate 1: chapter selection file round-trip + _select_targets honoring it
# ---------------------------------------------------------------------------


def _seed_run_dir(root: Path, n_chapters: int = 4) -> RunDirectory:
    run = RunDirectory(root)
    run.ensure()
    toc = [
        TocEntry(
            id=f"ch{i:02d}",
            title=f"Chapter {i}",
            source_ref=f"pp.{i}",
            kind=ChapterKind.CONTENT,
            word_count=1000,
        )
        for i in range(1, n_chapters + 1)
    ]
    manifest = BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path=str(root / "book.epub"),
            sha256="0" * 64,
            format="epub",
            ingested_at=datetime.now(UTC),
        ),
        metadata=BookMetadata(title="Test", authors=["Ada"]),
        structure=BookStructure(toc=toc),
    )
    run.save_book(manifest)
    for i in range(1, n_chapters + 1):
        run.save_raw_chapter(
            NormalizedChapter(
                chapter_id=f"ch{i:02d}",
                title=f"Chapter {i}",
                order=i,
                source_ref=f"pp.{i}",
                word_count=1000,
                text="lorem ipsum",
            )
        )
    return run


def test_map_selection_round_trip(tmp_path: Path) -> None:
    run = _seed_run_dir(tmp_path / "run")
    assert run.load_map_selection() is None

    run.save_map_selection(["ch01", "ch03"])
    assert run.load_map_selection() == ["ch01", "ch03"]
    assert run.map_selection_json.exists()


def test_map_selection_ignores_corrupt_file(tmp_path: Path) -> None:
    run = _seed_run_dir(tmp_path / "run")
    run.map_selection_json.write_text("not json")
    assert run.load_map_selection() is None


def test_select_targets_honors_map_selection(tmp_path: Path) -> None:
    run = _seed_run_dir(tmp_path / "run", n_chapters=4)
    run.save_map_selection(["ch02", "ch04"])

    selection = MapService().select_targets(MapInput(run_dir=run.root))
    ids = [c.chapter_id for c in selection.targets]
    assert ids == ["ch02", "ch04"]
    assert selection.selection_kept == 2
    assert selection.selection_total == 4


def test_select_targets_without_map_selection_returns_all(tmp_path: Path) -> None:
    run = _seed_run_dir(tmp_path / "run", n_chapters=3)
    selection = MapService().select_targets(MapInput(run_dir=run.root))
    ids = [c.chapter_id for c in selection.targets]
    assert ids == ["ch01", "ch02", "ch03"]
    assert selection.selection_kept is None


def test_select_targets_single_chapter_ignores_map_selection(tmp_path: Path) -> None:
    """The --chapter flag is a per-invocation override and bypasses the
    persisted selection entirely."""
    run = _seed_run_dir(tmp_path / "run", n_chapters=4)
    run.save_map_selection(["ch01"])
    selection = MapService().select_targets(MapInput(run_dir=run.root, chapter_id="ch03"))
    ids = [c.chapter_id for c in selection.targets]
    assert ids == ["ch03"]
