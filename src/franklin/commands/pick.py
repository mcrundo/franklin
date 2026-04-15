"""Interactive ``pick`` command — book picker + pre-run gates.

Scans configured directories for EPUB/PDF files, cross-references with
existing run directories so users can see which they've already
processed, then launches the pipeline via ``run_pipeline`` after a
two-gate confirmation (book pick + chapter selection / cost review).

Large module — the pick UX accumulated a lot of helpers. All of them
live here so ``cli.py`` isn't polluted with interactive-UI code.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from franklin.cli import (
    _print_estimate_callout,
    _resolve_run_dir,
    app,
    ingest,
)
from franklin.cli import console as console
from franklin.estimate import estimate_run
from franklin.ingest import UnsupportedFormatError
from franklin.picker import (
    ALL_FORMATS,
    DEFAULT_FORMATS,
    BookCandidate,
    default_search_dirs,
    discover_books,
)
from franklin.schema import BookManifest, ChapterKind, NormalizedChapter


@app.command(name="pick")
def pick_command(
    dirs: list[Path] = typer.Option(
        None,
        "--dir",
        "-d",
        help=(
            "Directory to scan. Pass multiple times to scan several locations. "
            "Defaults to FRANKLIN_BOOKS_DIR (colon-separated), or ~/Books, "
            "~/Media, ~/Downloads, ~/Documents."
        ),
    ),
    runs_base: Path = typer.Option(
        Path("./runs"),
        "--runs-base",
        help="Existing runs directory to cross-reference",
    ),
    pdf: bool = typer.Option(
        False,
        "--pdf",
        help="Include .pdf files in results (default is .epub only)",
    ),
    search: str | None = typer.Option(
        None,
        "--search",
        "-s",
        help="Case-insensitive substring filter against the filename",
    ),
    scan_home: bool = typer.Option(
        False,
        "--home",
        help="Scan $HOME recursively when no --dir is given (slow on large homedirs)",
    ),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Walk subdirectories"),
    limit: int = typer.Option(
        100, "--limit", help="Maximum number of candidates to display", min=1, max=500
    ),
    publish: bool = typer.Option(
        False,
        "--publish",
        help="After pipeline completes, interactively publish to GitHub",
    ),
) -> None:
    """Interactive picker for book files with run-state overlay.

    Defaults to scanning a list of common book locations for ``.epub``
    files and cross-references each against existing run directories so
    you can see which books you've already processed.

    Directory resolution:

    1. ``--dir`` options (one or more) — explicit override
    2. ``--home`` — scan ``$HOME`` recursively
    3. ``FRANKLIN_BOOKS_DIR`` env var (colon-separated, like ``$PATH``)
    4. Fallback: ``~/Books``, ``~/Media``, ``~/Downloads``, ``~/Documents``
       (those that exist)

    PDFs are excluded by default because they need the Tier 4 cleanup
    pass to match EPUB quality and tend to pollute listings. Pass
    ``--pdf`` to include them.

    ``--search <query>`` filters candidates by case-insensitive substring
    match on the filename — the 80/20 version of fuzzy search.
    """
    # Deferred so pick.py doesn't pull run_pipeline into cli module load order.
    from franklin.cli import run_pipeline

    search_dirs = _resolve_pick_dirs(dirs, scan_home=scan_home)
    if not search_dirs:
        console.print(
            "[red]error:[/red] no directories to scan — "
            "pass --dir, set FRANKLIN_BOOKS_DIR, or use --home"
        )
        raise typer.Exit(code=1)

    formats = ALL_FORMATS if pdf else DEFAULT_FORMATS

    candidates = discover_books(
        search_dirs,
        runs_base=runs_base,
        recursive=recursive,
        max_results=limit,
        formats=formats,
        query=search,
    )
    if not candidates:
        scope = f" ({len(search_dirs)} location(s))" if len(search_dirs) > 1 else ""
        query_note = f" matching '{search}'" if search else ""
        format_note = "epub/pdf" if pdf else "epub"
        console.print(f"[dim]no {format_note} files found{query_note}{scope}[/dim]")
        return

    console.print()
    rule_label = str(search_dirs[0]) if len(search_dirs) == 1 else f"{len(search_dirs)} locations"
    console.rule(f"[bold]franklin pick[/bold] — {rule_label}")
    if search:
        console.print(f"  [dim]filter: {search}[/dim]")
    if len(search_dirs) > 1:
        for d in search_dirs:
            console.print(f"  [dim]• {d}[/dim]")
        console.print()

    picked = _prompt_pick_candidate(candidates)
    if picked is None:
        console.print("[dim]cancelled[/dim]")
        return
    console.print()

    proceed = _pick_gate_one(picked.path)
    if not proceed:
        console.print("[dim]cancelled[/dim]")
        return

    console.print()
    console.print(f"[green]→[/green] launching franklin run on [cyan]{picked.path}[/cyan]")
    console.print()
    run_pipeline(
        book_path=picked.path,
        output=None,
        force=False,
        yes=True,  # Gate 1 already ingested and confirmed, don't re-prompt
        estimate=False,
        review=False,
        clean=False,
        push=False,
        repo=None,
        branch="main",
        create_pr=False,
        public=False,
        publish=publish,
    )


def _pick_gate_one(book_path: Path) -> bool:
    """Ingest, show estimate + chapter gate, persist selection. Returns proceed flag.

    The pick flow's first gate: after the user picks a book file, run
    ingest (cheap, local), show the predicted cost, and let the user
    narrow the chapter set before any paid stage starts. When the user
    chooses "edit", a multi-select drops in — all content chapters are
    pre-checked, spacebar toggles them off, Enter commits.

    Returns ``True`` if the run should proceed, ``False`` if the user
    cancelled. Any subset chosen here is persisted to
    ``map_selection.json`` so ``map`` picks it up during the pipeline.
    """
    run = _resolve_run_dir(book_path, None)
    run.ensure()

    # Gate 1 owns ingest so run_pipeline's ingest stage becomes a no-op
    # (guarded by book.json-exists). If ingest already ran in a prior
    # session we reuse that output instead of re-parsing.
    if not run.book_json.exists():
        try:
            ingest(
                book_path=book_path,
                output=run.root,
                yes_i_know_pdfs=False,
                clean=False,
                clean_concurrency=8,
                yes=False,
            )
        except UnsupportedFormatError as exc:
            console.print(f"[red]error:[/red] {exc}")
            return False

    manifest = run.load_book()
    chapters = [run.load_raw_chapter(cid) for cid in run.list_raw_chapters()]
    content_ids = [
        e.id
        for e in manifest.structure.toc
        if e.kind in (ChapterKind.CONTENT, ChapterKind.INTRODUCTION)
    ]

    pre_selected = run.load_map_selection()
    current_selection = set(pre_selected) if pre_selected is not None else set(content_ids)

    while True:
        console.print()
        _render_gate_estimate(manifest, chapters, current_selection)
        console.print()

        action = _prompt_gate_action()
        if action == "cancel":
            return False
        if action == "proceed":
            if set(current_selection) != set(content_ids):
                run.save_map_selection(sorted(current_selection))
            return True
        if action == "edit":
            edited = _prompt_chapter_selection(manifest, current_selection)
            if edited is None:
                continue  # user bailed out of the multi-select; loop back
            if not edited:
                console.print("[yellow]at least one chapter must be selected[/yellow]")
                continue
            current_selection = set(edited)


def _render_gate_estimate(
    manifest: BookManifest,
    chapters: list[NormalizedChapter],
    selected_ids: set[str],
) -> None:
    """Render the pre-map cost table, narrowed to ``selected_ids``."""
    result = estimate_run(manifest, chapters, allowed_ids=selected_ids)
    total_content = sum(
        1
        for e in manifest.structure.toc
        if e.kind in (ChapterKind.CONTENT, ChapterKind.INTRODUCTION)
    )

    console.rule(f"[bold]pre-map estimate[/bold] — {manifest.metadata.title}")
    if result.content_chapters == total_content:
        console.print(
            f"[bold]Chapters:[/bold] {result.content_chapters} "
            f"([dim]{result.total_words:,} words[/dim])"
        )
    else:
        console.print(
            f"[bold]Chapters:[/bold] {result.content_chapters} of {total_content} selected "
            f"([dim]{result.total_words:,} words[/dim])"
        )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Stage")
    table.add_column("Calls", justify="right")
    table.add_column("Input tok", justify="right")
    table.add_column("Output tok", justify="right")
    table.add_column("Cost (USD)", justify="right")
    for s in result.stages:
        table.add_row(
            s.stage,
            f"{s.calls:,}",
            f"{s.input_tokens:,}",
            f"{s.output_tokens:,}",
            f"${s.cost_usd:,.2f}",
        )
    table.add_row(
        "[bold]total[/bold]",
        f"{result.total_calls:,}",
        f"{result.total_input_tokens:,}",
        f"{result.total_output_tokens:,}",
        f"[bold]${result.total_cost_low_usd:,.2f} - ${result.total_cost_usd:,.2f}[/bold]",
    )
    console.print(table)
    _print_estimate_callout()


def _prompt_gate_action() -> str:
    """Ask the user to proceed / edit the chapter list / cancel.

    Falls back to a plain typer prompt when stdin isn't a TTY so scripted
    invocations still work (they'll just get the default "proceed").
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return "proceed"

    import questionary

    answer = questionary.select(
        "Proceed with the run?",
        choices=[
            questionary.Choice("Proceed", value="proceed"),
            questionary.Choice("Edit chapter selection", value="edit"),
            questionary.Choice("Cancel", value="cancel"),
        ],
    ).ask()
    if answer is None:  # Ctrl-C
        return "cancel"
    return str(answer)


def _prompt_chapter_selection(
    manifest: BookManifest, current_selection: set[str]
) -> list[str] | None:
    """Show a multi-select of content chapters, pre-checked to ``current_selection``.

    Returns the new set of chapter ids, or ``None`` if the user cancelled
    out of the multi-select (Ctrl-C). Only CONTENT / INTRODUCTION
    chapters are offered — front/back matter isn't worth spending tokens
    on and is already excluded from the default target set.
    """
    import questionary

    content_entries = [
        e
        for e in manifest.structure.toc
        if e.kind in (ChapterKind.CONTENT, ChapterKind.INTRODUCTION)
    ]
    if not content_entries:
        return None

    max_title = min(
        60,
        max((len(e.title) for e in content_entries), default=20),
    )
    choices = [
        questionary.Choice(
            title=(
                f"{e.id:<6} "
                f"{_truncate(e.title, max_title).ljust(max_title)}  "
                f"{e.word_count:>6,} words"
            ),
            value=e.id,
            checked=(e.id in current_selection),
        )
        for e in content_entries
    ]
    answer = questionary.checkbox(
        "Select chapters to map (space to toggle, ↵ to confirm)",
        choices=choices,
    ).ask()
    if answer is None:
        return None
    return [str(x) for x in answer]


def _prompt_pick_candidate(
    candidates: list[BookCandidate],
) -> BookCandidate | None:
    """Show an arrow-key picklist when interactive, fall back to a table otherwise.

    questionary gives us arrow + type-to-filter + Enter in the interactive
    branch; the fallback branch uses a Rich table and a numbered prompt so
    scripted use (redirected stdin, CI, etc.) still works without a TTY.
    """
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _questionary_pick(candidates)
    return _fallback_numbered_pick(candidates)


def _questionary_pick(candidates: list[BookCandidate]) -> BookCandidate | None:
    import questionary

    layout = _pick_column_layout(candidates)
    choices: list[Any] = []
    for c in candidates:
        title = _format_pick_row(c, layout)
        choices.append(questionary.Choice(title=title, value=c))
    choices.append(questionary.Choice(title="(cancel)", value=None))

    answer: BookCandidate | None = questionary.select(
        "Pick a book (type to filter, ↑↓ to move, ↵ to run)",
        choices=choices,
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()
    return answer  # None if user hit Ctrl-C or selected (cancel)


def _fallback_numbered_pick(
    candidates: list[BookCandidate],
) -> BookCandidate | None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", overflow="fold", style="cyan")
    table.add_column("Author", overflow="fold", style="dim")
    table.add_column("Year", justify="right", style="dim")
    table.add_column("Type", justify="center", style="dim")
    table.add_column("Size", justify="right", style="dim")
    table.add_column("Run state")
    for idx, c in enumerate(candidates, start=1):
        table.add_row(
            str(idx),
            c.display_name,
            c.author or "—",
            c.year or "—",
            c.extension,
            _format_size(c.size_bytes),
            _format_run_state(c),
        )
    console.print(table)
    console.print(f"[dim]{len(candidates)} candidate(s) shown[/dim]")
    console.print()

    choice: int = typer.prompt("Pick a number to run it (or 0 to cancel)", default=0, type=int)
    if choice == 0:
        return None
    if choice < 1 or choice > len(candidates):
        console.print(f"[red]invalid selection {choice}[/red]")
        raise typer.Exit(code=1)
    picked: BookCandidate = candidates[choice - 1]
    return picked


@dataclass(frozen=True)
class _PickLayout:
    """Column widths for a single picker invocation.

    Widths are computed once per render so every row aligns with every
    other row, and the whole line fits into the current terminal. The
    title column is elastic — it absorbs whatever space is left after
    the fixed columns are measured, then hard-truncates rows longer
    than that budget.
    """

    title: int
    author: int
    year: int
    size: int
    state: int


def _pick_column_layout(candidates: list[BookCandidate]) -> _PickLayout:
    import shutil

    term_width = shutil.get_terminal_size(fallback=(100, 24)).columns
    # Reserve a few chars for questionary's marker/pointer and a safety
    # margin — wrapping in a select prompt looks much worse than truncation.
    usable = max(60, term_width - 6)

    author_raw = max((len(c.author or "") for c in candidates), default=0)
    author_w = min(author_raw, 24)
    year_w = 4 if any(c.year for c in candidates) else 0
    size_w = max(
        (len(_format_size_plain(c.size_bytes)) for c in candidates),
        default=8,
    )
    state_w = max(
        (len(_format_run_state_plain(c)) for c in candidates),
        default=0,
    )

    gaps = 2 * sum(1 for w in (author_w, year_w, size_w, state_w) if w)
    fixed = author_w + year_w + size_w + state_w + gaps
    title_w = max(20, usable - fixed)
    return _PickLayout(
        title=title_w,
        author=author_w,
        year=year_w,
        size=size_w,
        state=state_w,
    )


def _format_pick_row(c: BookCandidate, layout: _PickLayout) -> str:
    parts = [_truncate(c.display_name, layout.title).ljust(layout.title)]
    if layout.author:
        parts.append(_truncate(c.author or "", layout.author).ljust(layout.author))
    if layout.year:
        parts.append((c.year or "").ljust(layout.year))
    parts.append(_format_size_plain(c.size_bytes).rjust(layout.size))
    parts.append(_format_run_state_plain(c).ljust(layout.state))
    return "  ".join(parts)


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _format_size_plain(size_bytes: int) -> str:
    """Size formatter for questionary choices (no Rich markup)."""
    if size_bytes == 0:
        return "—"
    size_f = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size_f < 1024:
            return f"{size_f:.1f} {unit}"
        size_f /= 1024
    return f"{size_f:.1f} TB"


def _format_run_state_plain(candidate: BookCandidate) -> str:
    """Plain-text run state for questionary choices."""
    run = candidate.existing_run
    if run is None:
        return "new"
    if run.last_stage == "assemble":
        grade = run.grade_letter or "—"
        return f"✓ assembled ({grade})"
    return f"⏳ partial ({run.last_stage or 'empty'})"


def _resolve_pick_dirs(explicit: list[Path] | None, *, scan_home: bool) -> list[Path]:
    """Pick the set of directories to scan, following the resolution order."""
    if explicit:
        return [d.expanduser() for d in explicit]
    if scan_home:
        return [Path.home()]
    return default_search_dirs()


def _format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes = int(size_bytes / 1024)
    return f"{size_bytes} TB"


def _format_run_state(candidate: BookCandidate) -> str:
    run = candidate.existing_run
    if run is None:
        return "[dim]new[/dim]"
    if run.last_stage == "assemble":
        grade = run.grade_letter or "—"
        return f"[green]assembled ({grade})[/green]"
    return f"[yellow]partial ({run.last_stage or 'empty'})[/yellow]"


__all__ = ["pick_command"]
