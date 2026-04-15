"""Franklin CLI entrypoint.

Exposes per-stage commands (ingest, map, plan, reduce, assemble) plus
a top-level `run` that chains them end-to-end.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from franklin.assembler import (
    BrokenLink,
    FrontmatterIssue,
    TemplateLeak,
)
from franklin.checkpoint import (
    RunDirectory,
    RunSummary,
    list_runs,
    slugify,
    summarize_run,
)
from franklin.doctor import CheckStatus, has_failures, run_checks
from franklin.errors import FriendlyError, format_friendly_error
from franklin.estimate import RunEstimate, estimate_run
from franklin.grading import RunGrade, grade_run
from franklin.ingest import UnsupportedFormatError, ingest_book
from franklin.inspector import (
    ChapterInspection,
    InspectError,
    InspectReport,
    inspect_run,
    report_to_json,
)
from franklin.installer import InstallError, install_plugin
from franklin.license import (
    LicenseError,
    LicenseHealth,
    LicenseStatus,
    ensure_license,
    refresh_revocations,
)
from franklin.license import login as license_login
from franklin.license import logout as license_logout
from franklin.license import status as license_status
from franklin.license import whoami as license_whoami
from franklin.mapper import DEFAULT_MODEL, build_user_prompt
from franklin.picker import (
    ALL_FORMATS,
    DEFAULT_FORMATS,
    BookCandidate,
    default_search_dirs,
    discover_books,
)
from franklin.planner import DEFAULT_MODEL as PLANNER_DEFAULT_MODEL
from franklin.publisher import PushError, push_plugin
from franklin.reducer import DEFAULT_MODEL as REDUCER_DEFAULT_MODEL
from franklin.review import apply_omissions, parse_omit_selection
from franklin.schema import (
    Artifact,
    ArtifactType,
    BookManifest,
    ChapterKind,
    NormalizedChapter,
    PlanManifest,
)
from franklin.secrets import MissingApiKeyError, ensure_anthropic_api_key
from franklin.services import (
    ArtifactNotFoundError,
    AssembleInput,
    AssembleResult,
    AssembleService,
    ChapterNotFoundError,
    InfoEvent,
    IngestInput,
    IngestService,
    ItemDone,
    ItemStart,
    MapInput,
    MapService,
    NoPlanError,
    NoSidecarsError,
    NoSidecarsForReduceError,
    PlanAlreadyExistsError,
    PlanInput,
    PlanService,
    PluginNotBuiltError,
    ProgressEvent,
    ReduceContext,
    ReduceInput,
    ReduceResult,
    ReduceService,
    RunNotIngestedError,
    StageFinish,
    StageStart,
    UnknownArtifactTypeError,
    WarningEvent,
)

_DEFAULT_MAP_CONCURRENCY = 8
_DEFAULT_REDUCE_CONCURRENCY = 3

app = typer.Typer(
    name="franklin",
    help="Turn technical books into Claude Code plugins.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        from franklin import __version__

        console.print(__version__)
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed franklin version and exit.",
    ),
) -> None:
    pass


license_app = typer.Typer(
    name="license",
    help="Manage your franklin license.",
    no_args_is_help=True,
)
app.add_typer(license_app, name="license")
runs_app = typer.Typer(
    name="runs",
    help="Inspect past pipeline runs.",
    no_args_is_help=True,
)
app.add_typer(runs_app, name="runs")
console = Console()

_PRICING_URL = "https://franklin.example.com/pricing"

# v0.1 of franklin ships fully free: every command is available regardless
# of license state. The license module stays in place (login, logout,
# whoami, status still work, and `_gate_pro_feature` is still exercised
# by tests that pin `_LICENSE_GATE_ENABLED=True`) so when a paid tier
# ships the gate can be re-enabled with a one-line flip here.
_LICENSE_GATE_ENABLED = False


def _gate_pro_feature(feature: str, command: str) -> None:
    """Check the license for a premium command, or exit with a friendly error.

    No-op when ``_LICENSE_GATE_ENABLED`` is False (the v0.1 default).
    When enabled, calls ensure_license(feature=...) and translates any
    LicenseError into a multi-line, ANSI-rendered explanation the user
    can act on. Never lets a stack trace reach stderr on license
    failure — the license module's messages go into the body of the
    panel, nothing else.
    """
    if not _LICENSE_GATE_ENABLED:
        return
    try:
        ensure_license(feature=feature)
    except LicenseError as exc:
        console.print()
        console.print(f"[red]✗[/red] [bold]franklin {command}[/bold] is a Pro feature")
        console.print()
        console.print("  This command requires a valid franklin license.")
        console.print(f"  [dim]Reason:[/dim] {exc}")
        console.print()
        console.print("  Upgrade or renew your license at:")
        console.print(f"    [cyan]{_PRICING_URL}[/cyan]")
        console.print()
        console.print("  If you already have a license, run:")
        console.print("    [cyan]franklin license login[/cyan]")
        console.print()
        raise typer.Exit(code=1) from exc


def _resolve_run_dir(book_path: Path, output: Path | None) -> RunDirectory:
    if output is not None:
        return RunDirectory(output)
    slug = _slug_from_metadata(book_path) or slugify(book_path.stem)
    return RunDirectory(Path.cwd() / "runs" / slug)


def _slug_from_metadata(book_path: Path) -> str | None:
    """Try to extract a clean title from book metadata for the slug."""
    ext = book_path.suffix.lower()
    title: str | None = None
    if ext == ".epub":
        from franklin.picker import _read_epub_opf_metadata

        try:
            meta = _read_epub_opf_metadata(book_path)
            title = meta.get("title")
        except Exception:
            pass
    elif ext == ".pdf":
        try:
            import pdfplumber

            with pdfplumber.open(book_path) as pdf:
                info = pdf.metadata or {}
                title = info.get("Title") or info.get("title")
        except Exception:
            pass
    if title and len(title.strip()) > 3:
        return slugify(title)
    return None


def _print_next_steps(
    *,
    run_dir: Path,
    pushed: bool,
    pushed_repo: str | None,
    plugin_name: str | None = None,
) -> None:
    """Render a tailored 'what to do next' block after assemble completes.

    The path a user takes depends on whether they already pushed:

    - Not pushed → guide them through local install first, then publish.
    - Pushed → show the GitHub URL and the install command end-users
      would run against the published plugin.

    Always surfaces the iteration loop (grade → reduce --force) and the
    review command, since those are the levers for "I don't like the
    output" that new users reach for but might not know about.
    """
    console.print()
    console.rule("[bold]Next steps[/bold]")

    if pushed and pushed_repo:
        console.print(
            f"  [green]✓[/green] published to [cyan]https://github.com/{pushed_repo}[/cyan]"
        )
        console.print()
        console.print("  [bold]Install from your published repo:[/bold]")
        console.print(f"    [cyan]claude plugin marketplace add {pushed_repo}[/cyan]")
        if plugin_name:
            console.print(f"    [cyan]claude plugin install {plugin_name}@{plugin_name}[/cyan]")
        console.print()
    else:
        console.print("  [bold]1.[/bold] Try it locally before publishing:")
        console.print(f"     [cyan]franklin install {run_dir} --scope local[/cyan]")
        console.print("     [dim](--scope user persists it; --scope local is per-session)[/dim]")
        console.print()
        console.print("  [bold]2.[/bold] When you're happy, publish to GitHub:")
        console.print(f"     [cyan]franklin publish {run_dir}[/cyan]")
        console.print("     [dim](interactive: picks repo name, owner, visibility for you)[/dim]")
        console.print()

    console.print("  [bold]Iterate on the output:[/bold]")
    console.print(f"     [cyan]franklin fix {run_dir}[/cyan]  — regenerate low-grade artifacts")
    console.print(f"     [cyan]franklin grade {run_dir}[/cyan]  — detailed grade card")
    console.print(f"     [cyan]franklin review {run_dir}[/cyan]  — prune artifacts you don't want")
    console.print(
        f"     [cyan]franklin reduce {run_dir} --artifact <id> --force[/cyan]  "
        "— regenerate a single file"
    )
    console.print()


def _print_friendly_error(friendly: FriendlyError, *, stage: str | None = None) -> None:
    """Render a FriendlyError as a Rich block with title/detail/suggestion."""
    prefix = f"{stage} stage — " if stage else ""
    console.print()
    console.print(f"[red]✗[/red] [bold red]{prefix}{friendly.title}[/bold red]")
    if friendly.detail:
        console.print(f"  [dim]{friendly.detail}[/dim]")
    console.print(f"  [yellow]→[/yellow] {friendly.suggestion}")
    if friendly.is_retryable:
        console.print("  [dim]this error is retryable[/dim]")
    console.print()


_STAGE_RETRY_COMMANDS: dict[str, str] = {
    "ingest": "franklin ingest {book_path} --output {run_dir}",
    "map": "franklin map {run_dir} --force",
    "plan": "franklin plan {run_dir} --force",
    "reduce": "franklin reduce {run_dir} --force",
    "assemble": "franklin assemble {run_dir}",
}


def _print_retry_hint(stage: str, run_root: Path) -> None:
    """Print a copy-pasteable retry command after a stage failure."""
    template = _STAGE_RETRY_COMMANDS.get(stage)
    if template:
        cmd = template.format(run_dir=run_root, book_path="<book>")
        console.print(f"  [dim]retry:[/dim] [cyan]{cmd}[/cyan]")
        console.print()


def _maybe_confirm_metadata(manifest: BookManifest, *, skip: bool) -> None:
    """Show detected metadata and ask the user to confirm or edit.

    EPUB metadata is notoriously wrong (especially for PDFs heuristically
    repackaged into EPUBs by scanning services). Surfacing it here gives
    the user a chance to correct title and author before the map stage
    turns those values into plugin identifiers that are hard to change.

    Skipped entirely in non-interactive contexts (no TTY) and when
    ``--yes`` is passed, so scripted ingests never block.
    """
    import sys

    if skip or not sys.stdin.isatty():
        return

    console.print()
    console.print("[bold]Detected metadata[/bold]")
    console.print(f"  Title:   [cyan]{manifest.metadata.title}[/cyan]")
    authors = ", ".join(manifest.metadata.authors) if manifest.metadata.authors else "(unknown)"
    console.print(f"  Authors: [cyan]{authors}[/cyan]")
    if manifest.metadata.publisher:
        console.print(f"  Publisher: [dim]{manifest.metadata.publisher}[/dim]")
    if manifest.metadata.published:
        console.print(f"  Published: [dim]{manifest.metadata.published}[/dim]")
    console.print()

    if typer.confirm("Is this correct?", default=True):
        return

    new_title = typer.prompt("  Title", default=manifest.metadata.title)
    authors_str = ", ".join(manifest.metadata.authors)
    new_authors_raw = typer.prompt(
        "  Authors (comma-separated)", default=authors_str or "(unknown)"
    )

    manifest.metadata.title = new_title.strip() or manifest.metadata.title
    new_authors = [
        a.strip() for a in new_authors_raw.split(",") if a.strip() and a.strip() != "(unknown)"
    ]
    manifest.metadata.authors = new_authors
    console.print(f"[green]✓[/green] updated to [cyan]{manifest.metadata.title}[/cyan]")


def _print_run_estimate(book_path: Path, *, include_cleanup: bool) -> None:
    """Parse the book locally and render a Rich table of predicted cost."""
    console.rule(f"[bold]franklin run --estimate[/bold] — {book_path.name}")
    console.print("  [dim]parsing book (no LLM calls, no disk writes)…[/dim]")
    try:
        book, chapters = ingest_book(book_path)
    except UnsupportedFormatError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    result: RunEstimate = estimate_run(book, chapters, include_cleanup=include_cleanup)

    console.print()
    console.print(f"[bold]Book:[/bold]       [cyan]{result.book_title}[/cyan]")
    console.print(
        f"[bold]Chapters:[/bold]   {result.content_chapters} content "
        f"([dim]{result.total_words:,} words[/dim])"
    )
    if include_cleanup:
        console.print("[bold]Options:[/bold]    [yellow]--clean[/yellow] included")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Stage")
    table.add_column("Model", style="dim")
    table.add_column("Calls", justify="right")
    table.add_column("Input tok", justify="right")
    table.add_column("Output tok", justify="right")
    table.add_column("Cost (USD)", justify="right")
    for s in result.stages:
        table.add_row(
            s.stage,
            s.model,
            f"{s.calls:,}",
            f"{s.input_tokens:,}",
            f"{s.output_tokens:,}",
            f"${s.cost_usd:,.2f}",
        )
    table.add_row(
        "[bold]total[/bold]",
        "",
        f"{result.total_calls:,}",
        f"{result.total_input_tokens:,}",
        f"{result.total_output_tokens:,}",
        f"[bold]${result.total_cost_low_usd:,.2f} - ${result.total_cost_usd:,.2f}[/bold]",
    )
    console.print(table)
    _print_estimate_callout()


_ESTIMATE_CALLOUT = (
    "[bold]This is a budget ceiling, not a prediction.[/bold]\n"
    "\n"
    "Franklin intentionally over-estimates so you're never surprised by your bill. "
    "Real runs typically cost significantly less because Anthropic's prompt caching "
    "gives a 90% discount on input tokens that repeat across chapters (system "
    "prompts, tool schemas, etc.), and the heuristics above assume worst-case "
    "output lengths that rarely happen in practice.\n"
    "\n"
    "Your actual spend is reported after each stage completes. If your real "
    "costs differ meaningfully from these estimates, let us know at "
    "github.com/mcrundo/franklin/issues -- it helps us calibrate."
)


def _print_estimate_callout() -> None:
    console.print()
    console.print(
        Panel(
            _ESTIMATE_CALLOUT,
            border_style="dim",
            padding=(0, 1),
        )
    )


def _maybe_prompt_resume(run_dir: Path, *, yes: bool) -> None:
    """If ``run_dir`` holds a partial run, show progress and confirm resume.

    Uses ``summarize_run`` so the check is free on a never-run dir (no
    book.json → stages_done == []). When there is work already done, we
    either auto-confirm (``--yes``) or ask the user. Answering no aborts
    with exit code 0 so scripts can tell "declined to resume" from
    "command crashed."
    """
    summary = summarize_run(run_dir)
    if not summary.stages_done:
        return

    all_stages = ("ingest", "map", "plan", "reduce", "assemble")
    done = set(summary.stages_done)
    console.print()
    console.print(f"[bold]Found existing run at[/bold] [cyan]{run_dir}[/cyan]")
    if summary.title:
        console.print(f"  [dim]{summary.title}[/dim]")
    for stage in all_stages:
        mark = "[green]✓[/green]" if stage in done else "[dim]○[/dim]"
        console.print(f"  {mark} {stage}")
    console.print()
    next_stage = next((s for s in all_stages if s not in done), None)
    if next_stage is None:
        console.print(
            "  [green]All stages complete.[/green] "
            "Use [cyan]--force[/cyan] to re-run from the start."
        )
        raise typer.Exit(code=0)

    console.print(
        f"  Will resume from [yellow]{next_stage}[/yellow]. "
        f"Use [cyan]--force[/cyan] to re-run from the start instead."
    )
    console.print()

    if yes:
        return
    if not typer.confirm("Resume this run?", default=True):
        console.print("[dim]aborted.[/dim]")
        raise typer.Exit(code=0)


@app.command()
def ingest(
    book_path: Path = typer.Argument(
        ..., exists=True, readable=True, help="Path to .epub or .pdf"
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Run directory (default: ./runs/<slug>)"
    ),
    yes_i_know_pdfs: bool = typer.Option(
        False,
        "--yes-i-know-pdfs",
        help="Suppress the PDF quality-caveat warning",
    ),
    clean: bool = typer.Option(
        False,
        "--clean",
        help="Run a Tier 4 LLM cleanup pass on extracted chapters (PDF only)",
    ),
    clean_concurrency: int = typer.Option(
        8,
        "--clean-concurrency",
        help="Number of concurrent LLM cleanup calls when --clean is set",
        min=1,
        max=32,
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive metadata confirmation prompt",
    ),
) -> None:
    """Parse a book file (EPUB or PDF) into normalized chapters and a partial book.json."""
    _do_ingest_stage(
        book_path=book_path,
        output=output,
        yes_i_know_pdfs=yes_i_know_pdfs,
        clean=clean,
        clean_concurrency=clean_concurrency,
        yes=yes,
    )


def _do_ingest_stage(
    *,
    book_path: Path,
    output: Path | None,
    yes_i_know_pdfs: bool,
    clean: bool,
    clean_concurrency: int,
    yes: bool,
) -> None:
    """Shared ingest implementation used by the ``ingest`` command and ``run_pipeline``."""
    run_dir = _resolve_run_dir(book_path, output).root

    is_pdf = book_path.suffix.lower() == ".pdf"
    if is_pdf and not yes_i_know_pdfs:
        _print_pdf_warning()

    if clean and not is_pdf:
        # The service also detects this, but the CLI prints the friendlier
        # dim note users are used to seeing, not a generic info event.
        console.print(
            "[dim]--clean is a no-op on EPUBs (they're already structurally clean)[/dim]"
        )

    console.print(f"[bold]Ingesting[/bold] {book_path}")

    def confirm(manifest: BookManifest) -> BookManifest:
        _maybe_confirm_metadata(manifest, skip=yes)
        return manifest

    renderer = _IngestRenderer(clean_concurrency=clean_concurrency)
    try:
        result = IngestService().run(
            IngestInput(
                book_path=book_path,
                run_dir=run_dir,
                clean=clean,
                clean_concurrency=clean_concurrency,
            ),
            progress=renderer.emit,
            metadata_confirm=confirm,
        )
    except UnsupportedFormatError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        renderer.close()

    _print_ingest_summary(RunDirectory(result.run_dir), result.manifest, result.chapters)


class _StageRenderer:
    """Base for the per-stage Rich progress bars.

    Handles the lifecycle that every stage renderer shares: filter
    events to the stage this renderer is listening on, open a standard
    Progress on ``stage_start``, advance on ``item_done``, close on
    ``stage_finish``. ``close()`` is defensive — if an exception
    short-circuits the service, any open Progress is stopped cleanly.

    Subclasses set ``stage`` and ``label`` (class attrs) and override
    the ``_on_*`` hooks for stage-specific text. Override ``emit`` to
    pick up out-of-band events (info/warning) the base doesn't handle.
    """

    stage: str = ""
    label: str = ""

    def __init__(self) -> None:
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None

    def emit(self, event: ProgressEvent) -> None:
        if event.stage != self.stage:
            return
        if isinstance(event, StageStart):
            self._open(event.total or 0)
        elif isinstance(event, ItemStart):
            self._on_item_start(event)
        elif isinstance(event, ItemDone):
            self._on_item_done(event)
        elif isinstance(event, StageFinish):
            self.close()
            self._on_stage_finish(event)

    def close(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._task_id = None

    def _open(self, total: int) -> None:
        progress = Progress(
            SpinnerColumn(),
            TextColumn(f"[bold]{self.label}[/bold]"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("·"),
            TimeElapsedColumn(),
            TextColumn("·"),
            TimeRemainingColumn(),
            TextColumn("· [dim]{task.fields[last]}[/dim]"),
            console=console,
            transient=False,
        )
        progress.start()
        self._progress = progress
        self._task_id = progress.add_task(self.label.lower(), total=total, last="starting…")

    def _update(self, *, advance: int = 0, last: str) -> None:
        if self._progress is not None and self._task_id is not None:
            self._progress.update(self._task_id, advance=advance, last=last)

    def _on_item_start(self, event: ItemStart) -> None:
        """Hook: update ``last`` when an item begins. Default is a no-op."""

    def _on_item_done(self, event: ItemDone) -> None:
        """Hook: advance the bar when an item finishes. Default is a no-op."""

    def _on_stage_finish(self, event: StageFinish) -> None:
        """Hook: print a summary after the progress closes. Default is a no-op."""


class _IngestRenderer(_StageRenderer):
    """Renders the optional Tier 4 cleanup sub-stage inside ingest.

    The ``ingest`` stage itself is line-at-a-time output (printed by
    ``_do_ingest_stage`` before the service runs); only the cleanup
    sub-stage owns a live Progress bar. We listen on ``cleanup`` so
    the base-class lifecycle fires for it, with a custom prelude that
    prints the rule + cost estimate before opening the bar.
    """

    stage = "cleanup"
    label = "Cleaning"

    def __init__(self, *, clean_concurrency: int) -> None:
        super().__init__()
        self._clean_concurrency = clean_concurrency

    def emit(self, event: ProgressEvent) -> None:
        if event.stage == "cleanup" and isinstance(event, WarningEvent):
            console.print(f"  [yellow]{event.message}[/yellow]")
            return
        if event.stage == "ingest" and isinstance(event, InfoEvent):
            # Library-level info like "Ingesting <path>" is already
            # printed by the CLI before the service runs; swallow to
            # avoid double-printing the same line.
            return
        super().emit(event)

    def _open(self, total: int) -> None:
        estimate = total * 0.08
        console.print()
        console.rule("[bold]Tier 4 cleanup[/bold]")
        console.print(f"  about to send {total} chapters to Claude for mechanical cleanup")
        console.print(f"  concurrency: [cyan]{self._clean_concurrency}[/cyan] in flight at once")
        console.print(
            f"  estimated cost: [yellow]~${estimate:.2f}[/yellow] total "
            "(actual will vary with chapter length)"
        )
        console.print()
        super()._open(total)

    def _on_item_done(self, event: ItemDone) -> None:
        marker = "⚠" if event.status == "fail" else "✓"
        suffix = " failed" if event.status == "fail" else ""
        self._update(advance=1, last=f"{marker} {event.item_id}{suffix}")

    def _on_stage_finish(self, event: StageFinish) -> None:
        console.print()
        console.print(f"[green]✓[/green] cleanup complete: {event.summary or ''}")


def _print_pdf_warning() -> None:
    console.print()
    console.print("[yellow]⚠ PDF support is experimental[/yellow]")
    console.print()
    console.print("  Franklin extracts PDFs using layout-aware heuristics. Quality depends")
    console.print("  heavily on the source PDF's structure. Common issues:")
    console.print()
    console.print("    - Code blocks may lose indentation if not set in a monospace font")
    console.print("    - Multi-column layouts may be jumbled")
    console.print("    - Chapter boundaries are taken from the PDF outline when available,")
    console.print("      otherwise inferred heuristically from font sizes")
    console.print()
    console.print("  For best results, prefer the EPUB edition when available. To suppress")
    console.print("  this warning for automation, re-run with [cyan]--yes-i-know-pdfs[/cyan].")
    console.print()


_KIND_STYLES: dict[ChapterKind, str] = {
    ChapterKind.CONTENT: "green",
    ChapterKind.INTRODUCTION: "bold green",
    ChapterKind.PART_DIVIDER: "yellow",
    ChapterKind.FRONT_MATTER: "dim",
    ChapterKind.BACK_MATTER: "dim",
}


def _print_ingest_summary(
    run: RunDirectory, manifest: BookManifest, chapters: list[NormalizedChapter]
) -> None:
    content_count = sum(
        1
        for e in manifest.structure.toc
        if e.kind in (ChapterKind.CONTENT, ChapterKind.INTRODUCTION)
    )

    console.print()
    console.print(f"[green]✓[/green] Run directory: {run.root}")
    console.print(f"  Title:     {manifest.metadata.title}")
    console.print(f"  Authors:   {', '.join(manifest.metadata.authors) or '—'}")
    console.print(f"  Chapters:  {len(chapters)} ({content_count} content)")
    console.print(f"  Words:     {manifest.structure.total_words:,}")
    console.print(f"  Code:      {'yes' if manifest.structure.has_code_examples else 'no'}")
    console.print()

    by_id = {c.chapter_id: c for c in chapters}
    table = Table(title="Chapters", show_header=True, header_style="bold")
    table.add_column("ID", style="cyan")
    table.add_column("Kind")
    table.add_column("Title")
    table.add_column("Words", justify="right")
    table.add_column("Code", justify="right")
    for entry in manifest.structure.toc:
        chapter = by_id[entry.id]
        style = _KIND_STYLES.get(entry.kind, "")
        table.add_row(
            entry.id,
            f"[{style}]{entry.kind.value}[/{style}]" if style else entry.kind.value,
            entry.title,
            f"{chapter.word_count:,}",
            str(len(chapter.code_blocks)),
        )
    console.print(table)


@app.command(name="map")
def map_chapters(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory from `franklin ingest`"
    ),
    chapter: str | None = typer.Option(
        None, "--chapter", "-c", help="Extract just this chapter_id (e.g. ch06)"
    ),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Anthropic model ID"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build and print the prompt without calling the API"
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-extract chapters that already have sidecars"
    ),
    concurrency: int = typer.Option(
        _DEFAULT_MAP_CONCURRENCY,
        "--concurrency",
        help="Number of concurrent LLM calls (default 8)",
        min=1,
        max=32,
    ),
) -> None:
    """Run the map stage: per-chapter structured extraction via the LLM."""
    _do_map_stage(
        run_dir=run_dir,
        chapter=chapter,
        model=model,
        dry_run=dry_run,
        force=force,
        concurrency=concurrency,
    )


def _do_map_stage(
    *,
    run_dir: Path,
    chapter: str | None,
    model: str,
    dry_run: bool,
    force: bool,
    concurrency: int,
) -> None:
    """Shared map implementation used by the ``map`` command and ``run_pipeline``."""
    service = MapService()
    params = MapInput(
        run_dir=run_dir,
        chapter_id=chapter,
        model=model,
        force=force,
        concurrency=concurrency,
    )

    try:
        selection = service.select_targets(params)
    except RunNotIngestedError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except ChapterNotFoundError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not selection.targets:
        console.print("[yellow]no chapters to extract[/yellow]")
        raise typer.Exit(code=0)

    if dry_run:
        _dry_run_prompt(selection.run, selection.manifest, selection.targets[0])
        return

    try:
        ensure_anthropic_api_key()
    except MissingApiKeyError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    renderer = _MapRenderer()
    try:
        service.run(params, progress=renderer.emit)
    finally:
        renderer.close()


def _dry_run_prompt(run: RunDirectory, manifest: BookManifest, chapter: NormalizedChapter) -> None:
    prompt = build_user_prompt(manifest, chapter)
    console.print(f"[bold]Dry run[/bold] — prompt for {chapter.chapter_id} ({chapter.title})")
    console.print(f"  run dir: {run.root}")
    console.print(f"  chars:   {len(prompt):,}")
    console.print(f"  approx tokens: {len(prompt) // 4:,}")
    console.print()
    console.print(prompt)


class _MapRenderer(_StageRenderer):
    """Translate MapService progress events into the Rich bar."""

    stage = "map"
    label = "Mapping"

    def emit(self, event: ProgressEvent) -> None:
        if event.stage == "map" and isinstance(event, InfoEvent):
            console.print(f"  [dim]{event.message}[/dim]")
            return
        super().emit(event)

    def _on_item_start(self, event: ItemStart) -> None:
        self._update(last=f"-> {event.item_id}")

    def _on_item_done(self, event: ItemDone) -> None:
        detail = f" ({event.detail})" if event.detail else ""
        self._update(advance=1, last=f"v {event.item_id}{detail}")

    def _on_stage_finish(self, event: StageFinish) -> None:
        # Preserve the blank-line-then-summary layout users see today.
        # The service's summary already covers counts + tokens + cost,
        # so we just wrap it in the familiar green check prefix.
        console.print()
        console.print(f"[green]✓[/green] map stage complete: {event.summary or ''}")


@app.command(name="plan")
def plan_pipeline(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory with sidecars"
    ),
    model: str = typer.Option(
        PLANNER_DEFAULT_MODEL, "--model", help="Anthropic model ID for the planner"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build and print the plan prompt without calling the API"
    ),
    force: bool = typer.Option(
        False, "--force", help="Regenerate plan.json even if it already exists"
    ),
) -> None:
    """Design the plugin architecture from the distilled sidecars."""
    _do_plan_stage(run_dir=run_dir, model=model, dry_run=dry_run, force=force)


def _do_plan_stage(
    *,
    run_dir: Path,
    model: str,
    dry_run: bool,
    force: bool,
) -> None:
    """Shared plan implementation used by the ``plan`` command and ``run_pipeline``."""
    service = PlanService()
    params = PlanInput(run_dir=run_dir, model=model, force=force)

    try:
        context = service.prepare(params)
    except RunNotIngestedError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except NoSidecarsError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except PlanAlreadyExistsError as exc:
        console.print(
            f"[yellow]plan.json already exists at {exc.plan_path}[/yellow]\n"
            "  use --force to regenerate, or open it directly to edit"
        )
        raise typer.Exit(code=1) from exc

    if dry_run:
        prompt = service.build_prompt(context.manifest, context.sidecars)
        console.print("[bold]Dry run[/bold] — plan prompt")
        console.print(f"  chars: {len(prompt):,}")
        console.print(f"  approx tokens: {len(prompt) // 4:,}")
        console.print(f"  sidecars: {len(context.sidecars)}")
        console.print()
        console.print(prompt)
        return

    try:
        ensure_anthropic_api_key()
    except MissingApiKeyError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[bold]Designing plugin[/bold] for [cyan]{context.manifest.metadata.title}[/cyan] "
        f"from {len(context.sidecars)} sidecars using [dim]{model}[/dim]"
    )
    from rich.live import Live
    from rich.spinner import Spinner

    spinner = Spinner("aesthetic", text=" [dim]thinking...[/dim]")
    with Live(spinner, console=console, refresh_per_second=10, transient=True):
        result = service.run(params)

    _print_plan_summary(context.run, result.plan, result.input_tokens, result.output_tokens)


def _print_plan_summary(
    run: RunDirectory, plan: PlanManifest, input_tokens: int, output_tokens: int
) -> None:
    by_type: dict[str, list[str]] = {}
    for artifact in plan.artifacts:
        by_type.setdefault(artifact.type.value, []).append(artifact.path)

    console.print()
    console.print(f"[green]✓[/green] plan saved to {run.plan_json}")
    console.print(f"  [dim]{input_tokens:,} input tokens / {output_tokens:,} output tokens[/dim]")
    console.print()
    console.print(f"[bold]Plugin:[/bold] {plan.plugin.name} [dim]v{plan.plugin.version}[/dim]")
    if plan.plugin.description:
        console.print(f"  {plan.plugin.description}")
    console.print()
    console.print("[bold]Rationale:[/bold]")
    for line in plan.planner_rationale.splitlines():
        console.print(f"  {line}")
    console.print()

    counts_table = Table(title=f"Artifacts ({len(plan.artifacts)})", show_header=True)
    counts_table.add_column("Type", style="cyan")
    counts_table.add_column("Count", justify="right")
    for type_name in (t.value for t in ArtifactType):
        paths = by_type.get(type_name, [])
        if paths:
            counts_table.add_row(type_name, str(len(paths)))
    console.print(counts_table)
    console.print()

    tree_table = Table(title="File tree", show_header=True)
    tree_table.add_column("Path", style="cyan")
    tree_table.add_column("Brief")
    tree_table.add_column("Est. tokens", justify="right")
    for artifact in plan.artifacts:
        tree_table.add_row(
            artifact.path,
            artifact.brief[:80] + ("…" if len(artifact.brief) > 80 else ""),
            f"{artifact.estimated_output_tokens:,}" if artifact.estimated_output_tokens else "—",
        )
    console.print(tree_table)

    if plan.skipped_artifact_types:
        console.print()
        skip_table = Table(title="Skipped", show_header=True, header_style="dim")
        skip_table.add_column("Type", style="dim")
        skip_table.add_column("Reason", style="dim")
        for skip in plan.skipped_artifact_types:
            skip_table.add_row(skip.type, skip.reason)
        console.print(skip_table)

    if plan.coherence_rules:
        console.print()
        console.print("[bold]Coherence rules:[/bold]")
        for rule in plan.coherence_rules:
            console.print(f"  • {rule}")

    console.print()
    console.print(
        f"Review the plan at [cyan]{run.plan_json}[/cyan] and edit as needed.\n"
        "Next: [bold]franklin reduce[/bold] (coming soon) to generate each artifact."
    )


@app.command(name="reduce")
def reduce_pipeline(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory with plan.json"
    ),
    artifact: str | None = typer.Option(
        None,
        "--artifact",
        "-a",
        help="Generate just this artifact id",
    ),
    type_filter: str | None = typer.Option(
        None,
        "--type",
        "-t",
        help="Generate only artifacts of this type (skill, reference, command, agent)",
    ),
    model: str = typer.Option(
        REDUCER_DEFAULT_MODEL, "--model", help="Anthropic model ID for generation"
    ),
    force: bool = typer.Option(
        False, "--force", help="Regenerate artifacts whose output file already exists"
    ),
    concurrency: int = typer.Option(
        _DEFAULT_REDUCE_CONCURRENCY,
        "--concurrency",
        help="Number of concurrent LLM calls (default 3, lower preserves prompt cache)",
        min=1,
        max=16,
    ),
) -> None:
    """Generate each artifact file from the plan using its feeds_from slice."""
    _do_reduce_stage(
        run_dir=run_dir,
        artifact=artifact,
        type_filter=type_filter,
        model=model,
        force=force,
        concurrency=concurrency,
    )


def _do_reduce_stage(
    *,
    run_dir: Path,
    artifact: str | None,
    type_filter: str | None,
    model: str,
    force: bool,
    concurrency: int,
) -> None:
    """Shared reduce implementation used by the ``reduce`` command and ``run_pipeline``."""
    service = ReduceService()
    params = ReduceInput(
        run_dir=run_dir,
        artifact_id=artifact,
        type_filter=type_filter,
        model=model,
        force=force,
        concurrency=concurrency,
    )

    try:
        context = service.prepare(params)
    except NoPlanError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except NoSidecarsForReduceError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        targets = service.select_artifacts(
            context.plan, artifact_id=artifact, type_filter=type_filter
        )
    except ArtifactNotFoundError as exc:
        console.print(
            f"[red]error:[/red] no artifact with id {exc.artifact_id!r} in plan "
            f"(available: {', '.join(exc.available)})"
        )
        raise typer.Exit(code=1) from exc
    except UnknownArtifactTypeError as exc:
        console.print(
            f"[red]error:[/red] unknown artifact type {exc.requested!r} "
            f"(valid: {', '.join(exc.valid)})"
        )
        raise typer.Exit(code=1) from exc

    if not targets:
        console.print("[yellow]no artifacts to generate[/yellow]")
        raise typer.Exit(code=0)

    try:
        ensure_anthropic_api_key()
    except MissingApiKeyError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _invoke_reduce(
        service,
        context=context,
        targets=targets,
        model=model,
        force=force,
        concurrency=concurrency,
    )


def _invoke_reduce(
    service: ReduceService,
    *,
    context: ReduceContext,
    targets: list[Artifact],
    model: str,
    force: bool,
    concurrency: int = _DEFAULT_REDUCE_CONCURRENCY,
) -> ReduceResult:
    """Render the pre-stage header, run the service, render the summary.

    Shared by the ``reduce`` command and the ``fix`` regeneration loop;
    both paths render the same output. Targets that already exist are
    filtered inside the service, so the ``len(to_generate)`` vs
    ``len(targets)`` distinction is intentionally blurred in the
    header — users saw "Generating N artifacts" for the whole target
    set historically, and that's what we keep here.
    """
    output_root = context.run.output_dir / context.plan.plugin.name
    console.print(
        f"[bold]Generating[/bold] {len(targets)} artifacts for "
        f"[cyan]{context.plan.plugin.name}[/cyan] using [dim]{model}[/dim]"
    )
    console.print(f"  output: {output_root}")

    renderer = _ReduceRenderer(output_root=output_root)
    try:
        result = service.generate(
            context,
            targets,
            model=model,
            force=force,
            concurrency=concurrency,
            progress=renderer.emit,
        )
    finally:
        renderer.close()
    return result


class _ReduceRenderer(_StageRenderer):
    """Translate ReduceService events into the Rich bar + multi-line summary."""

    stage = "reduce"
    label = "Reducing"

    def __init__(self, *, output_root: Path) -> None:
        super().__init__()
        self._output_root = output_root

    def _on_item_start(self, event: ItemStart) -> None:
        label = event.label or event.item_id
        self._update(last=f"-> {label}")

    def _on_item_done(self, event: ItemDone) -> None:
        if event.status == "fail":
            marker = f"[red]x {event.item_id}: {event.detail}[/red]"
        else:
            detail = f" ({event.detail})" if event.detail else ""
            marker = f"v {event.item_id}{detail}"
        self._update(advance=1, last=marker)

    def _on_stage_finish(self, event: StageFinish) -> None:
        console.print()
        # The service's summary carries counts + tokens + cost in one
        # line; split it into the two-line green-check layout users see.
        if event.summary:
            head, _, tail = event.summary.partition(" · ")
            console.print(f"[green]✓[/green] reduce stage complete: {head}")
            if tail:
                console.print(f"  [dim]{tail}[/dim]")
            console.print(f"  plugin tree: [cyan]{self._output_root}[/cyan]")


@app.command(name="assemble")
def assemble_pipeline(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory with a generated plugin"
    ),
    zip_archive: bool = typer.Option(
        False,
        "--zip",
        help="Also produce a distributable .zip archive of the plugin tree",
    ),
) -> None:
    """Assemble the generated plugin tree: write plugin.json and report."""
    _do_assemble_stage(run_dir=run_dir, zip_archive=zip_archive)


def _do_assemble_stage(*, run_dir: Path, zip_archive: bool) -> None:
    """Shared assemble implementation used by the ``assemble`` command and ``run_pipeline``."""
    try:
        result = AssembleService().run(AssembleInput(run_dir=run_dir, zip_archive=zip_archive))
    except NoPlanError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except PluginNotBuiltError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]Assembling[/bold] [cyan]{result.plugin_root.name}[/cyan]")
    _render_assemble_result(result)


def _render_assemble_result(result: AssembleResult) -> None:
    """Print the assemble report from a structured AssembleResult.

    Kept as a standalone helper so the ``run`` orchestrator in RUB-103
    can reuse it when composing services.
    """
    plugin_root = result.plugin_root
    console.print(f"  plugin root: {plugin_root}")
    console.print()

    console.print(f"[green]✓[/green] wrote {result.manifest_path.relative_to(plugin_root)}")
    console.print(f"[green]✓[/green] wrote {result.readme_path.relative_to(plugin_root)}")
    if result.gitignore_written:
        console.print("[green]✓[/green] wrote .gitignore")

    console.print(f"  {result.total_files} files total ({result.markdown_files} markdown)")

    if result.broken_links:
        _print_broken_links(plugin_root, result.broken_links)
    else:
        console.print("[green]✓[/green] all markdown links resolve")

    if result.template_leaks:
        _print_template_leaks(plugin_root, result.template_leaks)
    else:
        console.print("[green]✓[/green] no unfilled template placeholders")

    if result.frontmatter_issues:
        _print_frontmatter_issues(plugin_root, result.frontmatter_issues)
    else:
        console.print("[green]✓[/green] all frontmatter blocks are valid")

    console.print()
    if result.issue_count:
        console.print(f"[yellow]⚠ assemble finished with {result.issue_count} issue(s)[/yellow]")
    else:
        console.print(f"[green]✓[/green] assemble complete: {plugin_root}")

    console.print()
    _print_grade_card(result.grade, plan_name=plugin_root.name)
    console.print(f"  [dim]metrics: {result.metrics_path}[/dim]")

    if result.archive_path is not None:
        size_kb = result.archive_path.stat().st_size / 1024
        console.print(
            f"[green]✓[/green] packaged [cyan]{result.archive_path.name}[/cyan] "
            f"({size_kb:,.1f} KB) at {result.archive_path}"
        )


def _print_grade_card(grade: RunGrade, *, plan_name: str) -> None:
    """Render the run grade card to the console."""
    letter_color = {
        "A": "bold green",
        "A-": "bold green",
        "B+": "green",
        "B": "green",
        "B-": "yellow",
        "C+": "yellow",
        "C": "yellow",
        "C-": "yellow",
        "D": "red",
        "F": "bold red",
    }.get(grade.letter, "white")

    console.rule(f"[bold]Run Grade: [{letter_color}]{grade.letter}[/{letter_color}][/bold]")
    console.print(
        f"  Score:      [cyan]{grade.composite_score:.2f}[/cyan] "
        f"(structural {grade.structural_average:.2f}, "
        f"coverage {grade.coverage_fraction:.2f})"
    )
    v = grade.validator_totals
    validator_bits: list[str] = []
    validator_bits.append(
        "[green]✓[/green] links" if v.broken_links == 0 else f"[red]✗[/red] {v.broken_links} links"
    )
    validator_bits.append(
        "[green]✓[/green] templates"
        if v.template_leaks == 0
        else f"[red]✗[/red] {v.template_leaks} leaks"
    )
    validator_bits.append(
        "[green]✓[/green] frontmatter"
        if v.frontmatter_issues == 0
        else f"[red]✗[/red] {v.frontmatter_issues} frontmatter"
    )
    console.print(f"  Validation: {'  '.join(validator_bits)}")
    console.print(
        f"  Artifacts:  {len(grade.artifact_grades)} graded "
        f"across {v.markdown_files} markdown files"
    )
    if grade.warnings:
        console.print(f"  Warnings:   [yellow]{len(grade.warnings)}[/yellow]")
        for w in grade.warnings:
            console.print(f"    - {w}")
    if grade.failed_stages:
        console.print(f"  [red]Failed stages:[/red] {', '.join(grade.failed_stages)}")

    lowest = grade.lowest_graded
    if lowest and lowest[0].score < 1.0:
        console.print()
        console.print("  [bold]Lowest-graded artifacts:[/bold]")
        for g in lowest:
            console.print(f"    - {g.path:<42} [dim]{g.letter}[/dim] ({g.score:.2f})")
            for failed in g.failed_checks[:3]:
                console.print(f"        [dim]- missed: {failed}[/dim]")
        worst = lowest[0]
        console.print()
        console.print(
            f"  Next: [cyan]franklin reduce <run> --artifact {worst.artifact_id} --force[/cyan]"
        )


def _print_broken_links(plugin_root: Path, broken: list[BrokenLink]) -> None:
    missing = [b for b in broken if b.kind == "missing"]
    placeholder = [b for b in broken if b.kind == "placeholder"]

    if missing:
        console.print()
        console.print(f"[red]✗[/red] {len(missing)} broken link(s):")
        table = Table(show_header=True, header_style="bold red")
        table.add_column("Source file", style="cyan", overflow="fold")
        table.add_column("Line", justify="right")
        table.add_column("Target path", overflow="fold")
        table.add_column("Link text", overflow="fold")
        for link in missing:
            source = str(link.source_file.relative_to(plugin_root))
            table.add_row(source, str(link.line_number), link.target_path, link.link_text)
        console.print(table)

    if placeholder:
        console.print()
        console.print(f"[red]✗[/red] {len(placeholder)} unfilled placeholder link(s):")
        table = Table(show_header=True, header_style="bold red")
        table.add_column("Source file", style="cyan", overflow="fold")
        table.add_column("Line", justify="right")
        table.add_column("Placeholder target", overflow="fold")
        for link in placeholder:
            source = str(link.source_file.relative_to(plugin_root))
            table.add_row(source, str(link.line_number), link.target_path)
        console.print(table)


def _print_template_leaks(plugin_root: Path, leaks: list[TemplateLeak]) -> None:
    console.print()
    console.print(f"[red]✗[/red] {len(leaks)} unfilled template placeholder(s):")
    table = Table(show_header=True, header_style="bold red")
    table.add_column("Source file", style="cyan", overflow="fold")
    table.add_column("Line", justify="right")
    table.add_column("Placeholder", overflow="fold")
    table.add_column("Context", overflow="fold")
    for leak in leaks:
        source = str(leak.source_file.relative_to(plugin_root))
        table.add_row(source, str(leak.line_number), leak.placeholder, leak.context)
    console.print(table)


def _print_frontmatter_issues(plugin_root: Path, issues: list[FrontmatterIssue]) -> None:
    console.print()
    console.print(f"[red]✗[/red] {len(issues)} frontmatter issue(s):")
    table = Table(show_header=True, header_style="bold red")
    table.add_column("Source file", style="cyan", overflow="fold")
    table.add_column("Category")
    table.add_column("Kind")
    table.add_column("Message", overflow="fold")
    for issue in issues:
        source = str(issue.source_file.relative_to(plugin_root))
        table.add_row(source, issue.category, issue.kind, issue.message)
    console.print(table)


@app.command(name="review")
def review_command(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory containing plan.json"
    ),
) -> None:
    """Review the planned artifacts and omit any you don't want to generate.

    Reads plan.json, prints a numbered table of proposed artifacts, and
    prompts for comma-separated indices to omit. The reduced plan is
    saved back to plan.json in place; nothing else on disk is touched.
    Re-runnable — each review pass starts from the current plan state.
    """
    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(f"[red]error:[/red] no plan.json in {run_dir} — run `franklin plan` first")
        raise typer.Exit(code=1)

    plan = run.load_plan()
    if not plan.artifacts:
        console.print("[dim]plan has no artifacts to review[/dim]")
        return

    _print_review_table(plan.artifacts)

    while True:
        raw = typer.prompt(
            "Indices to omit (e.g. 1,3 or 2-4), blank to keep all",
            default="",
            show_default=False,
        )
        try:
            indices = parse_omit_selection(raw, total=len(plan.artifacts))
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        break

    if not indices:
        console.print("[green]✓[/green] keeping all artifacts; plan unchanged")
        return

    omit_ids = [plan.artifacts[i - 1].id for i in indices]
    result = apply_omissions(plan, omit_ids)

    console.print()
    console.print("[bold]About to omit:[/bold]")
    for artifact in result.omitted:
        console.print(f"  [red]-[/red] {artifact.path}  [dim]({artifact.id})[/dim]")
    console.print()
    console.print(
        f"[bold]Keeping {result.kept_count} artifact(s)[/bold] (was {len(plan.artifacts)})"
    )

    if not typer.confirm("Save the reduced plan?", default=True):
        console.print("[dim]no changes written[/dim]")
        return

    run.save_plan(result.plan)
    console.print(f"[green]✓[/green] plan.json updated: {run.plan_json}")


def _print_review_table(artifacts: list[Artifact]) -> None:
    console.print()
    console.rule("[bold]Review proposed artifacts[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Type", style="dim")
    table.add_column("Path", style="cyan", overflow="fold")
    table.add_column("Brief", overflow="fold")
    table.add_column("Feeds from", style="dim", overflow="fold")
    for idx, a in enumerate(artifacts, start=1):
        table.add_row(
            str(idx),
            a.type.value,
            a.path,
            a.brief,
            ", ".join(a.feeds_from) if a.feeds_from else "[dim](none)[/dim]",
        )
    console.print(table)
    console.print()


@app.command(name="grade")
def grade_command(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory to grade"
    ),
    output_json: bool = typer.Option(
        False, "--json", help="Emit the RunGrade as JSON instead of a Rich report"
    ),
) -> None:
    """Grade an assembled run and print a detailed report.

    Local diagnostic only — no LLM, no network, no writes. Re-runs every
    validator fresh so hand-edits and post-hoc regenerations are reflected
    immediately. Exit code is always 0 regardless of grade; the command
    reports, it doesn't gate.
    """
    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(f"[red]error:[/red] no plan.json in {run_dir} — run `franklin plan` first")
        raise typer.Exit(code=1)

    plan = run.load_plan()
    plugin_root = run.output_dir / plan.plugin.name
    if not plugin_root.exists():
        console.print(
            f"[red]error:[/red] no assembled plugin tree at {plugin_root} — "
            "run `franklin assemble` first"
        )
        raise typer.Exit(code=1)

    grade = grade_run(run_dir)

    if output_json:
        import json

        console.print_json(json.dumps(grade.to_metrics_dict(), default=str))
        return

    _print_detailed_grade_report(grade, plan_name=plan.plugin.name)


def _print_detailed_grade_report(grade: RunGrade, *, plan_name: str) -> None:
    """Render a full per-artifact breakdown for the grade command."""
    letter_color = {
        "A": "bold green",
        "A-": "bold green",
        "B+": "green",
        "B": "green",
        "B-": "yellow",
        "C+": "yellow",
        "C": "yellow",
        "C-": "yellow",
        "D": "red",
        "F": "bold red",
    }.get(grade.letter, "white")

    console.print()
    console.print(f"[bold]Plugin:[/bold] [cyan]{plan_name}[/cyan]")
    console.print(
        f"[bold]Grade:[/bold]  [{letter_color}]{grade.letter}[/{letter_color}] "
        f"([cyan]{int(grade.composite_score * 100)}/100[/cyan])"
    )
    console.print()
    console.print("[bold]Validation[/bold]")
    v = grade.validator_totals
    for label, count in (
        ("broken links", v.broken_links),
        ("template leaks", v.template_leaks),
        ("frontmatter issues", v.frontmatter_issues),
    ):
        icon = "[green]✓[/green]" if count == 0 else "[red]✗[/red]"
        console.print(f"  {icon} {count} {label}")
    console.print()

    wired = sum(1 for g in grade.artifact_grades if g.score > 0)
    console.print("[bold]Coverage[/bold]")
    console.print(
        f"  {wired}/{len(grade.artifact_grades)} artifacts graded, "
        f"feeds_from coverage: [cyan]{grade.coverage_fraction:.0%}[/cyan]"
    )
    console.print()

    if grade.artifact_grades:
        console.print(f"[bold]Artifacts ({len(grade.artifact_grades)})[/bold]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Path", overflow="fold", style="cyan")
        table.add_column("Grade", justify="center")
        table.add_column("Score", justify="right")
        table.add_column("Missed checks", overflow="fold", style="dim")
        for g in grade.artifact_grades:
            total = len(g.checks) or 1
            passed = sum(1 for c in g.checks if c.passed)
            missed = ", ".join(g.failed_checks) if g.failed_checks else ""
            table.add_row(g.path, g.letter, f"{passed}/{total}", missed)
        console.print(table)
        console.print()

    lowest = [g for g in grade.lowest_graded if g.score < 1.0]
    if lowest:
        console.print("[bold]Lowest grades[/bold]")
        for rank, g in enumerate(lowest, start=1):
            console.print(f"  {rank}. {g.path:<48} [dim]{g.letter}[/dim]")
        console.print()
        console.print("[bold]Suggested next steps[/bold]")
        for g in lowest:
            console.print(
                f"  [cyan]franklin reduce {grade.run_dir} "
                f"--artifact {g.artifact_id} --force[/cyan]"
            )
        console.print()

    if grade.failed_stages:
        console.print(f"[red]Failed stages:[/red] {', '.join(grade.failed_stages)}")


@app.command(name="diff")
def diff_command(
    run_a: Path = typer.Argument(..., exists=True, file_okay=False, help="First run directory"),
    run_b: Path = typer.Argument(..., exists=True, file_okay=False, help="Second run directory"),
) -> None:
    """Compare two runs side-by-side: grade delta, per-artifact score changes.

    Useful for evaluating the impact of prompt improvements, chapter
    selection changes, or force-regenerated artifacts. Both runs must
    have been assembled (metrics.json present).
    """
    grade_a = grade_run(run_a)
    grade_b = grade_run(run_b)

    # Header
    console.rule("[bold]Run comparison[/bold]")
    console.print(f"  [dim]A:[/dim] {run_a}")
    console.print(f"  [dim]B:[/dim] {run_b}")
    console.print()

    # Overall grade delta
    delta = grade_b.composite_score - grade_a.composite_score
    direction = "[green]+[/green]" if delta > 0 else "[red][/red]" if delta < 0 else "="
    console.print(
        f"  Grade:  {grade_a.letter} ({grade_a.composite_score:.2f}) "
        f"-> {grade_b.letter} ({grade_b.composite_score:.2f})  "
        f"{direction}{abs(delta):.2f}"
    )
    console.print(
        f"  Struct: {grade_a.structural_average:.2f} -> {grade_b.structural_average:.2f}"
    )
    console.print()

    # Per-artifact comparison
    scores_a = {g.path: g for g in grade_a.artifact_grades}
    scores_b = {g.path: g for g in grade_b.artifact_grades}
    all_paths = sorted(set(scores_a.keys()) | set(scores_b.keys()))

    table = Table(show_header=True, header_style="bold")
    table.add_column("Artifact", style="cyan", overflow="fold")
    table.add_column("A", justify="center")
    table.add_column("B", justify="center")
    table.add_column("Delta", justify="right")
    table.add_column("Notes", style="dim")

    improved = 0
    regressed = 0
    unchanged = 0

    for path in all_paths:
        ga = scores_a.get(path)
        gb = scores_b.get(path)

        if ga and gb:
            d = gb.score - ga.score
            if abs(d) < 0.01:
                unchanged += 1
                continue  # skip unchanged in table
            if d > 0:
                improved += 1
                delta_str = f"[green]+{d:.2f}[/green]"
            else:
                regressed += 1
                delta_str = f"[red]{d:.2f}[/red]"

            # What checks changed?
            failed_a = set(ga.failed_checks)
            failed_b = set(gb.failed_checks)
            fixed = failed_a - failed_b
            broken = failed_b - failed_a
            notes_parts: list[str] = []
            if fixed:
                notes_parts.append(f"fixed: {', '.join(sorted(fixed))}")
            if broken:
                notes_parts.append(f"regressed: {', '.join(sorted(broken))}")

            table.add_row(path, ga.letter, gb.letter, delta_str, "; ".join(notes_parts))
        elif ga and not gb:
            table.add_row(path, ga.letter, "—", "", "removed in B")
        elif gb and not ga:
            table.add_row(path, "—", gb.letter, "", "new in B")

    if improved + regressed > 0:
        console.print(table)
    console.print()
    console.print(
        f"  [green]{improved} improved[/green]  "
        f"[red]{regressed} regressed[/red]  "
        f"[dim]{unchanged} unchanged[/dim]"
    )

    # Content size comparison
    run_a_dir = RunDirectory(run_a)
    run_b_dir = RunDirectory(run_b)
    if run_a_dir.plan_json.exists() and run_b_dir.plan_json.exists():
        plan_a = run_a_dir.load_plan()
        plan_b = run_b_dir.load_plan()
        root_a = run_a_dir.output_dir / plan_a.plugin.name
        root_b = run_b_dir.output_dir / plan_b.plugin.name
        if root_a.exists() and root_b.exists():
            size_a = sum(f.stat().st_size for f in root_a.rglob("*.md"))
            size_b = sum(f.stat().st_size for f in root_b.rglob("*.md"))
            console.print(
                f"  Content: {size_a // 1024}KB -> {size_b // 1024}KB "
                f"({'+' if size_b >= size_a else ''}{(size_b - size_a) // 1024}KB)"
            )

    # Cost comparison
    costs_a = run_a_dir.load_costs()
    costs_b = run_b_dir.load_costs()
    if costs_a or costs_b:
        cost_a = sum(float(str(e.get("cost_usd", 0))) for e in costs_a)
        cost_b = sum(float(str(e.get("cost_usd", 0))) for e in costs_b)
        if cost_a > 0 or cost_b > 0:
            console.print(f"  Cost:    ${cost_a:.2f} -> ${cost_b:.2f}")
    console.print()


@app.command(name="validate")
def validate_command(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory to validate"
    ),
) -> None:
    """Quick quality check on generated artifacts without re-grading.

    Reads each artifact file and checks for common prompt-compliance
    issues: references missing problem framing, commands with long
    descriptions, agents without structured checklists. Faster than
    ``franklin grade`` and more targeted than ``franklin fix`` — useful
    as a sanity check before publishing.
    """
    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(f"[red]error:[/red] no plan.json in {run_dir}")
        raise typer.Exit(code=1)

    plan = run.load_plan()
    plugin_root = run.output_dir / plan.plugin.name
    if not plugin_root.exists():
        console.print(f"[red]error:[/red] no plugin at {plugin_root}")
        raise typer.Exit(code=1)

    from franklin.grading import _RUBRICS

    issues: list[tuple[str, str, list[str]]] = []

    for artifact in plan.artifacts:
        path = plugin_root / artifact.path
        if not path.exists():
            issues.append((artifact.path, artifact.type.value, ["file missing"]))
            continue

        rubric = _RUBRICS.get(artifact.type, [])
        if not rubric:
            continue

        text = path.read_text(encoding="utf-8")
        failed = [name for name, fn in rubric if not fn(text, path)]
        if failed:
            issues.append((artifact.path, artifact.type.value, failed))

    if not issues:
        console.print(f"[green]✓[/green] All {len(plan.artifacts)} artifacts pass validation.")
        return

    console.rule("[bold yellow]Validation issues[/bold yellow]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Artifact", style="cyan", overflow="fold")
    table.add_column("Type", style="dim")
    table.add_column("Failed checks")
    for path_str, art_type, failed in issues:
        table.add_row(path_str, art_type, ", ".join(failed))
    console.print(table)
    console.print()
    console.print(
        f"  {len(issues)} artifact(s) with issues. "
        f"Run [cyan]franklin fix {run_dir}[/cyan] to regenerate."
    )


_FIX_SCORE_THRESHOLD = 0.83  # below B


@app.command(name="fix")
def fix_command(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory to fix"),
    model: str = typer.Option(
        REDUCER_DEFAULT_MODEL, "--model", help="Anthropic model ID for regeneration"
    ),
    threshold: float = typer.Option(
        _FIX_SCORE_THRESHOLD,
        "--threshold",
        help="Score threshold — artifacts below this are candidates (0.0-1.0)",
    ),
) -> None:
    """Interactively fix low-grade artifacts.

    Grades the run, shows artifacts below the threshold, lets you pick
    which ones to regenerate, re-runs reduce on those, re-assembles,
    and shows the new grade. Loops until you're satisfied or everything
    is above the threshold.
    """
    import sys

    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(f"[red]error:[/red] no plan.json in {run_dir}")
        raise typer.Exit(code=1)

    plan = run.load_plan()
    book = run.load_book()
    plugin_root = run.output_dir / plan.plugin.name
    if not plugin_root.exists():
        console.print(f"[red]error:[/red] no plugin at {plugin_root} — run assemble first")
        raise typer.Exit(code=1)

    sidecar_ids = [p.stem for p in sorted(run.chapters_dir.glob("*.json"))]
    sidecars = {cid: run.load_sidecar(cid) for cid in sidecar_ids}
    artifact_by_id = {a.id: a for a in plan.artifacts}

    while True:
        grade = grade_run(run_dir)
        weak = [g for g in grade.artifact_grades if g.score < threshold]
        if not weak:
            console.print(
                f"[green]✓[/green] All artifacts score [bold]{threshold:.2f}+[/bold] "
                f"(grade: {grade.letter}). Nothing to fix."
            )
            break

        console.print()
        console.rule("[bold]Artifacts below threshold[/bold]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Artifact", style="cyan")
        table.add_column("Grade", justify="center")
        table.add_column("Score", justify="right")
        table.add_column("Missed checks")
        for idx, g in enumerate(weak, start=1):
            table.add_row(
                str(idx),
                g.path,
                g.letter,
                f"{g.score:.2f}",
                ", ".join(g.failed_checks[:3]),
            )
        console.print(table)
        console.print(
            f"  [dim]{len(weak)} artifact(s) below {threshold:.2f} "
            f"(run grade: {grade.letter} / {grade.composite_score:.2f})[/dim]"
        )
        console.print()

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            console.print("[dim]non-interactive — regenerating all[/dim]")
            to_fix = weak
        else:
            import questionary

            action = questionary.select(
                "What would you like to do?",
                choices=[
                    questionary.Choice(f"Regenerate all {len(weak)}", value="all"),
                    questionary.Choice("Pick which ones to regenerate", value="pick"),
                    questionary.Choice("Done — accept current grades", value="done"),
                ],
            ).ask()

            if action is None or action == "done":
                break
            if action == "all":
                to_fix = weak
            else:
                picks = questionary.checkbox(
                    "Select artifacts to regenerate (space to toggle, enter to confirm)",
                    choices=[
                        questionary.Choice(
                            f"{g.path} ({g.letter})",
                            value=g,
                            checked=True,
                        )
                        for g in weak
                    ],
                ).ask()
                if not picks:
                    continue
                to_fix = picks

        # Regenerate selected artifacts
        targets = []
        for g in to_fix:
            art = artifact_by_id.get(g.artifact_id)
            if art:
                targets.append(art)

        if not targets:
            console.print("[yellow]no matching artifacts found in plan[/yellow]")
            break

        console.print()
        _invoke_reduce(
            ReduceService(),
            context=ReduceContext(run=run, plan=plan, book=book, sidecars=sidecars),
            targets=targets,
            model=model,
            force=True,
            concurrency=_DEFAULT_REDUCE_CONCURRENCY,
        )

        # Re-assemble to get fresh grade
        console.print()
        assemble_pipeline(run_dir=run_dir, zip_archive=False)

    console.print()


@app.command(name="inspect")
def inspect_command(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory to inspect"
    ),
    chapter: str | None = typer.Option(
        None,
        "--chapter",
        "-c",
        help="Print full prose and code blocks for one chapter (by chapter_id, e.g. ch05)",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of the rich terminal report",
    ),
) -> None:
    """Preview a run's ingest output before committing to the paid stages."""
    try:
        report = inspect_run(run_dir)
    except InspectError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        print(report_to_json(report))
        return

    if chapter is not None:
        _render_single_chapter(report, chapter)
        return

    _render_inspect_summary(report)


def _render_inspect_summary(report: InspectReport) -> None:
    book = report.book
    console.rule(f"[bold]franklin inspect[/bold] — {book.metadata.title}")
    authors = ", ".join(book.metadata.authors) if book.metadata.authors else "—"
    console.print(f"  Authors:  {authors}")
    console.print(f"  Format:   {book.source.format}")
    console.print(f"  Chapters: {report.total_chapters} ({report.content_chapters} content)")
    console.print(
        f"  Words:    {report.total_words:,} total, "
        f"avg {report.avg_content_words:,} per content chapter"
    )
    console.print()

    for inspection in report.chapters:
        _render_chapter_block(inspection)

    if report.anomalies:
        console.rule("[bold yellow]Anomalies[/bold yellow]")
        for anomaly in report.anomalies:
            console.print(
                f"  [yellow]⚠[/yellow] [cyan]{anomaly.chapter_id}[/cyan] "
                f"[dim]{anomaly.kind}:[/dim] {anomaly.message}"
            )
        console.print()
    else:
        console.rule("[bold green]No anomalies detected[/bold green]")
        console.print()


def _render_chapter_block(inspection: ChapterInspection) -> None:
    chapter = inspection.chapter
    toc = inspection.toc_entry
    mark = " [yellow]⚠[/yellow]" if inspection.anomalies else ""
    header = (
        f"── [cyan]{chapter.chapter_id}[/cyan] · {toc.kind.value} · "
        f"{chapter.word_count:,} words · "
        f"{len(chapter.code_blocks)} code blocks ──{mark}"
    )
    console.print(header)
    console.print(f"  Title: {chapter.title}")

    prose_sample = chapter.text[:400].rstrip()
    if prose_sample:
        console.print()
        console.print("  [dim]Prose sample:[/dim]")
        for line in prose_sample.splitlines():
            console.print(f"    {line}")
        if len(chapter.text) > 400:
            console.print("    [dim]...[/dim]")

    longest = inspection.longest_code_block
    if longest:
        console.print()
        console.print(f"  [dim]Longest code block ({len(longest):,} chars):[/dim]")
        sample = longest[:300]
        for line in sample.splitlines():
            console.print(f"    {line}")
        if len(longest) > 300:
            console.print("    [dim]...[/dim]")

    for anomaly in inspection.anomalies:
        console.print(f"  [yellow]⚠ {anomaly.kind}:[/yellow] {anomaly.message}")
    console.print()


def _render_single_chapter(report: InspectReport, chapter_id: str) -> None:
    target: ChapterInspection | None = None
    for inspection in report.chapters:
        if inspection.chapter.chapter_id == chapter_id:
            target = inspection
            break

    if target is None:
        available = ", ".join(c.chapter.chapter_id for c in report.chapters)
        console.print(
            f"[red]error:[/red] chapter {chapter_id!r} not found (available: {available})"
        )
        raise typer.Exit(code=1)

    chapter = target.chapter
    toc = target.toc_entry
    console.rule(f"[bold]{chapter.chapter_id}[/bold] — {chapter.title}")
    console.print(f"  Kind:        {toc.kind.value}")
    console.print(f"  Confidence:  {toc.kind_confidence:.2f} ({toc.kind_reason})")
    console.print(f"  Source:      {chapter.source_ref}")
    console.print(f"  Words:       {chapter.word_count:,}")
    console.print(f"  Code blocks: {len(chapter.code_blocks)}")
    console.print()

    console.rule("[bold]Full text[/bold]")
    console.print(chapter.text)
    console.print()

    if chapter.code_blocks:
        console.rule(f"[bold]Code blocks ({len(chapter.code_blocks)})[/bold]")
        for i, code_block in enumerate(chapter.code_blocks, start=1):
            console.print(
                f"[dim]── code-block-{i}"
                + (f" ({code_block.language})" if code_block.language else "")
                + " ──[/dim]"
            )
            console.print(code_block.code)
            console.print()

    if target.anomalies:
        console.rule("[bold yellow]Anomalies[/bold yellow]")
        for anomaly in target.anomalies:
            console.print(f"  [yellow]⚠ {anomaly.kind}:[/yellow] {anomaly.message}")


@app.command(name="batch")
def batch_command(
    books: list[Path] = typer.Argument(
        ..., exists=True, readable=True, help="Book files to process"
    ),
    clean: bool = typer.Option(False, "--clean", help="Run Tier 4 LLM cleanup for PDFs"),
) -> None:
    """Process multiple books end-to-end, one after another.

    Runs the full pipeline (ingest -> map -> plan -> reduce -> assemble)
    for each book with all interactive gates auto-confirmed. Results are
    printed as a summary table at the end.

    Each book gets its own run directory under ./runs/<slug>.
    """
    results: list[tuple[str, str, str]] = []

    for i, book_path in enumerate(books, start=1):
        console.rule(f"[bold]Book {i}/{len(books)}: {book_path.name}[/bold]")
        console.print()

        try:
            is_pdf = book_path.suffix.lower() == ".pdf"
            run_pipeline(
                book_path=book_path,
                output=None,
                force=False,
                yes=True,
                estimate=False,
                review=False,
                clean=clean and is_pdf,
                push=False,
                repo=None,
                branch="main",
                create_pr=False,
                public=False,
                publish=False,
            )
            run = _resolve_run_dir(book_path, None)
            grade = grade_run(run.root)
            cost = sum(float(str(e.get("cost_usd", 0))) for e in run.load_costs())
            results.append((book_path.name, grade.letter, f"${cost:.2f}"))
        except (typer.Exit, Exception) as exc:
            results.append((book_path.name, "FAIL", str(exc)[:40]))
            console.print(f"[red]✗ {book_path.name} failed: {exc}[/red]")
        console.print()

    # Summary table
    console.rule("[bold]Batch summary[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Book", style="cyan")
    table.add_column("Grade", justify="center")
    table.add_column("Cost")
    for name, grade_letter, cost_or_err in results:
        table.add_row(name, grade_letter, cost_or_err)
    console.print(table)


@app.command(name="run")
def run_pipeline(
    book_path: Path = typer.Argument(
        ..., exists=True, readable=True, help="Path to .epub or .pdf"
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Run directory (default: ./runs/<slug>)"
    ),
    force: bool = typer.Option(False, "--force", help="Re-run stages whose outputs already exist"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Auto-confirm resuming a partial run without prompting",
    ),
    estimate: bool = typer.Option(
        False,
        "--estimate",
        help="Predict token counts and cost without running the paid stages",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Pause between plan and reduce to review and omit artifacts",
    ),
    clean: bool = typer.Option(
        False,
        "--clean",
        help="Run Tier 4 LLM cleanup during ingest (PDF only; also shown in --estimate totals)",
    ),
    push: bool = typer.Option(
        False, "--push", help="After assemble, push the plugin to GitHub (requires --repo)"
    ),
    repo: str | None = typer.Option(
        None, "--repo", help="GitHub repository as owner/name (required with --push)"
    ),
    branch: str = typer.Option(
        "main", "--branch", help="Target branch to push to (only with --push)"
    ),
    create_pr: bool = typer.Option(
        False, "--pr", help="Open a PR against main after pushing (only with --push)"
    ),
    public: bool = typer.Option(
        False, "--public", help="Create the repo as public (only with --push)"
    ),
    publish: bool = typer.Option(
        False,
        "--publish",
        help="After assemble, interactively publish to GitHub (guided: name, owner, visibility)",
    ),
) -> None:
    """Run the full pipeline end-to-end: ingest → map → plan → reduce → assemble."""
    _validate_push_flags(push=push, repo=repo, branch=branch, create_pr=create_pr, public=public)

    run = _resolve_run_dir(book_path, output)

    if estimate:
        _print_run_estimate(book_path, include_cleanup=clean)
        return

    if run.root.exists() and not force:
        _maybe_prompt_resume(run.root, yes=yes)

    run.ensure()

    # Auto-suggest --clean for PDFs when the user didn't pass it.
    import sys

    is_pdf = book_path.suffix.lower() == ".pdf"
    if is_pdf and not clean and not yes and sys.stdin.isatty():
        import questionary

        suggest = questionary.confirm(
            "This is a PDF. Run with --clean for better extraction quality?",
            default=True,
        ).ask()
        if suggest is None:
            raise typer.Exit(code=0)
        if suggest:
            clean = True

    console.rule(f"[bold]franklin run[/bold] — {book_path.name}")
    console.print(f"  run directory: {run.root}")
    if force:
        console.print("  [yellow]--force[/yellow]: re-running existing stages")
    if push:
        console.print(f"  [yellow]--push[/yellow]: publish to {repo} on branch {branch}")
    console.print()

    # Compose the stage services directly. Each lambda builds the right
    # input and invokes the shared `_do_*_stage` helper used by the
    # per-stage Typer commands — no Typer dispatch detour.
    stages: list[tuple[str, Callable[[], None]]] = [
        (
            "ingest",
            lambda: _do_ingest_stage(
                book_path=book_path,
                output=run.root,
                yes_i_know_pdfs=False,
                clean=clean,
                clean_concurrency=8,
                yes=yes,
            ),
        ),
        (
            "map",
            lambda: _do_map_stage(
                run_dir=run.root,
                chapter=None,
                model=DEFAULT_MODEL,
                dry_run=False,
                force=force,
                concurrency=_DEFAULT_MAP_CONCURRENCY,
            ),
        ),
        (
            "plan",
            lambda: _do_plan_stage(
                run_dir=run.root,
                model=PLANNER_DEFAULT_MODEL,
                dry_run=False,
                force=force,
            ),
        ),
        (
            "reduce",
            lambda: _do_reduce_stage(
                run_dir=run.root,
                artifact=None,
                type_filter=None,
                model=REDUCER_DEFAULT_MODEL,
                force=force,
                concurrency=_DEFAULT_REDUCE_CONCURRENCY,
            ),
        ),
        ("assemble", lambda: _do_assemble_stage(run_dir=run.root, zip_archive=False)),
    ]
    # Gate 2: post-map summary before the Opus plan call. Always inserted
    # unless --yes auto-confirms (scripted use) or --force (rebuilding).
    if not (yes and force):
        stages.insert(
            2,  # after map, before plan
            ("gate-2", lambda: _run_gate_two(run.root)),
        )
    if review:
        stages.insert(
            4 if not (yes and force) else 3,  # after plan, before reduce
            ("review", lambda: review_command(run_dir=run.root)),
        )
    if push:
        # `repo` is guaranteed non-None here by _validate_push_flags.
        assert repo is not None
        stages.append(
            (
                "push",
                lambda: push_command(
                    run_dir=run.root,
                    repo=repo,
                    branch=branch,
                    create_pr=create_pr,
                    public=public,
                ),
            )
        )

    for name, fn in stages:
        # `plan` is the only stage whose standalone command refuses to run
        # when its output already exists. In run's resume-on-disk semantics
        # that should be a skip, not a failure.
        if name == "plan" and run.plan_json.exists() and not force:
            console.rule(f"[dim]skip {name} — plan.json exists (use --force to regenerate)[/dim]")
            console.print()
            continue
        # `ingest` is deterministic and the pick flow's Gate 1 may have
        # already produced book.json + raw/ before calling run_pipeline.
        # Skip when the artifacts are already on disk, same shape as the
        # plan-skip above, so the pipeline resumes cleanly.
        if name == "ingest" and run.book_json.exists() and not force:
            console.rule(f"[dim]skip {name} — book.json exists (use --force to regenerate)[/dim]")
            console.print()
            continue
        # Gate 2 is a pre-plan checkpoint — skip if plan already exists
        # (resume case), and don't print the bold stage header since
        # the gate renders its own.
        if name == "gate-2" and run.plan_json.exists() and not force:
            continue
        if name == "gate-2":
            fn()
            continue

        console.rule(f"[bold cyan]{name}[/bold cyan]")
        try:
            fn()
        except typer.Exit as exc:
            if exc.exit_code:
                console.print(f"[red]✗ {name} stage failed (exit code {exc.exit_code})[/red]")
                _print_retry_hint(name, run.root)
                raise typer.Exit(code=exc.exit_code) from exc
            # exit_code 0 is a graceful "nothing to do" — continue to next stage.
        except Exception as exc:
            friendly = format_friendly_error(exc)
            _print_friendly_error(friendly, stage=name)
            _print_retry_hint(name, run.root)
            raise typer.Exit(code=friendly.exit_code) from exc
        console.print()

    console.rule("[bold green]pipeline complete[/bold green]")
    console.print(f"[green]✓[/green] {run.root}")

    if publish:
        console.print()
        publish_command(run_dir=run.root)
    else:
        plan_for_steps = run.load_plan() if run.plan_json.exists() else None
        _print_next_steps(
            run_dir=run.root,
            pushed=push,
            pushed_repo=repo,
            plugin_name=plan_for_steps.plugin.name if plan_for_steps else None,
        )


def _validate_push_flags(
    *, push: bool, repo: str | None, branch: str, create_pr: bool, public: bool
) -> None:
    """Reject invalid --push flag combinations before any work happens.

    `--repo` is required with `--push`, and the push-only modifiers
    (`--branch` when non-default, `--pr`, `--public`) are rejected on
    their own. The goal is to fail the command before spending any
    tokens or touching disk if the user's invocation doesn't make sense.
    """
    if push and not repo:
        console.print("[red]error:[/red] --push requires --repo owner/name")
        raise typer.Exit(code=2)

    if not push:
        stray: list[str] = []
        if repo is not None:
            stray.append("--repo")
        if branch != "main":
            stray.append("--branch")
        if create_pr:
            stray.append("--pr")
        if public:
            stray.append("--public")
        if stray:
            console.print(f"[red]error:[/red] {', '.join(stray)} can only be used with --push")
            raise typer.Exit(code=2)


@app.command(name="push")
def push_command(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory with an assembled plugin"
    ),
    repo: str = typer.Option(..., "--repo", help="GitHub repository as owner/name"),
    branch: str = typer.Option("main", "--branch", help="Target branch to push to"),
    create_pr: bool = typer.Option(
        False, "--pr", help="Open a pull request against main after pushing"
    ),
    public: bool = typer.Option(
        False, "--public", help="Create the repo as public (default: private)"
    ),
) -> None:
    """Push the assembled plugin tree to a GitHub repository."""
    _gate_pro_feature("push", "push")

    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(f"[red]error:[/red] no plan.json in {run_dir} — run `franklin plan` first")
        raise typer.Exit(code=1)

    plan = run.load_plan()
    plugin_root = run.output_dir / plan.plugin.name
    if not plugin_root.exists():
        console.print(
            f"[red]error:[/red] no assembled plugin at {plugin_root} — "
            "run `franklin reduce` and `franklin assemble` first"
        )
        raise typer.Exit(code=1)

    commit_message = f"franklin: assemble {plan.plugin.name} v{plan.plugin.version}"

    console.rule(f"[bold]franklin push[/bold] — {plan.plugin.name}")
    console.print(f"  plugin root: {plugin_root}")
    console.print(f"  repo:        {repo}")
    console.print(f"  branch:      {branch}")
    console.print(f"  visibility:  {'public' if public else 'private'}")
    if create_pr:
        console.print("  --pr:        open a pull request")
    console.print()

    try:
        result = push_plugin(
            plugin_root,
            repo=repo,
            branch=branch,
            create_pr=create_pr,
            public=public,
            commit_message=commit_message,
        )
    except PushError as exc:
        console.print(f"[red]✗ push failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print()
    if result.created_repo:
        console.print(f"[green]✓[/green] created repository {result.repo_url}")
    else:
        console.print(f"[green]✓[/green] updated repository {result.repo_url}")
    console.print(f"  pushed branch [cyan]{result.branch}[/cyan] via [dim]{result.backend}[/dim]")
    if result.pr_url:
        console.print(f"  [green]✓[/green] pull request: {result.pr_url}")

    # Patch the README's install section with the real repo name.
    readme_path = plugin_root / "README.md"
    if readme_path.exists():
        readme_text = readme_path.read_text()
        if "claude plugin marketplace add owner/repo" in readme_text:
            readme_text = readme_text.replace(
                "claude plugin marketplace add owner/repo",
                f"claude plugin marketplace add {repo}",
            )
            readme_text = readme_text.replace(
                "\n*Replace `owner/repo` with the GitHub repository "
                "after publishing with `franklin push`.*\n",
                "\n",
            )
            readme_path.write_text(readme_text)
            console.print(f"  [green]✓[/green] updated README.md install section with {repo}")


@app.command(name="publish")
def publish_command(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory with an assembled plugin"
    ),
) -> None:
    """Interactively publish a plugin to GitHub.

    Guides you through grade review, repo naming, owner selection, and
    visibility — then pushes and prints the install command. Designed
    to be the last step after ``franklin run`` or ``franklin fix``.
    """
    import subprocess

    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(f"[red]error:[/red] no plan.json in {run_dir}")
        raise typer.Exit(code=1)

    plan = run.load_plan()
    plugin_root = run.output_dir / plan.plugin.name
    if not plugin_root.exists():
        console.print(f"[red]error:[/red] no plugin at {plugin_root} — run assemble first")
        raise typer.Exit(code=1)

    # ---- grade check + auto-fix ----------------------------------------

    grade = grade_run(run_dir)
    _print_grade_card(grade, plan_name=plan.plugin.name)
    console.print()

    weak = [g for g in grade.artifact_grades if g.score < _FIX_SCORE_THRESHOLD]
    if weak:
        import questionary

        fix_action = questionary.select(
            f"{len(weak)} artifact(s) scored below B. Fix before publishing?",
            choices=[
                questionary.Choice(f"Fix all {len(weak)} now", value="fix"),
                questionary.Choice("Publish anyway", value="skip"),
                questionary.Choice("Cancel", value="cancel"),
            ],
        ).ask()

        if fix_action is None or fix_action == "cancel":
            console.print("[dim]cancelled[/dim]")
            return

        if fix_action == "fix":
            # Run the fix loop
            fix_command(
                run_dir=run_dir,
                model=REDUCER_DEFAULT_MODEL,
                threshold=_FIX_SCORE_THRESHOLD,
            )
            # Re-grade after fix
            grade = grade_run(run_dir)

    # ---- resolve GitHub user + orgs ------------------------------------

    try:
        gh_user = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        console.print("[red]error:[/red] could not resolve GitHub user — is `gh` authenticated?")
        raise typer.Exit(code=1) from exc

    owners = [gh_user]
    try:
        orgs_output = subprocess.run(
            ["gh", "api", "user/orgs", "--jq", ".[].login"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if orgs_output:
            owners.extend(orgs_output.splitlines())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass  # no orgs, that's fine

    # ---- interactive prompts -------------------------------------------

    import questionary

    default_name = plan.plugin.name
    repo_name = questionary.text(
        "Repository name",
        default=default_name,
    ).ask()
    if not repo_name:
        console.print("[dim]cancelled[/dim]")
        return

    if len(owners) == 1:
        owner = owners[0]
    else:
        owner = questionary.select(
            "Where should we publish?",
            choices=[questionary.Choice(o, value=o) for o in owners]
            + [questionary.Choice("Cancel", value=None)],
        ).ask()
        if owner is None:
            console.print("[dim]cancelled[/dim]")
            return

    visibility = questionary.select(
        "Visibility",
        choices=[
            questionary.Choice("Public (anyone can install)", value="public"),
            questionary.Choice("Private", value="private"),
        ],
    ).ask()
    if visibility is None:
        console.print("[dim]cancelled[/dim]")
        return

    repo = f"{owner}/{repo_name}"
    is_public = visibility == "public"

    vis_label = "public" if is_public else "private"
    console.print()
    console.print(f"  [bold]Publishing to:[/bold] [cyan]{repo}[/cyan] ({vis_label})")
    console.print(f"  [bold]Grade:[/bold] {grade.letter} ({grade.composite_score:.2f})")
    console.print()

    # ---- push ----------------------------------------------------------

    push_command(
        run_dir=run_dir,
        repo=repo,
        branch="main",
        create_pr=False,
        public=is_public,
    )

    console.print()
    console.rule("[bold green]Published[/bold green]")
    console.print()
    console.print("  Install with:")
    console.print(f"    [cyan]claude plugin marketplace add {repo}[/cyan]")
    console.print(f"    [cyan]claude plugin install {plan.plugin.name}@{plan.plugin.name}[/cyan]")
    console.print()


_VALID_INSTALL_SCOPES: tuple[str, ...] = ("user", "project", "local")


@app.command(name="install")
def install_command(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Run directory with an assembled plugin"
    ),
    scope: str = typer.Option(
        "user",
        "--scope",
        help="Install scope: user (default), project, or local",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing plugin of the same name"
    ),
) -> None:
    """Install the assembled plugin tree into Claude Code.

    - **user** (default): copy into the franklin-owned local marketplace at
      ~/.franklin/marketplace/<plugin-name>/, then activate via
      `/plugin marketplace add` + `/plugin install`. Persistent, available
      in every Claude Code session.
    - **project**: same install as user scope, but the activation sequence
      records the plugin in the current project's `.claude/settings.json`
      so it loads only when Claude Code is run from that project root.
      Git-committed and team-shared.
    - **local**: no filesystem writes. Prints the `claude --plugin-dir`
      command for a single-session dev load against the assembled tree.
      `--force` is ignored since nothing is written.
    """
    _gate_pro_feature("install", "install")

    if scope not in _VALID_INSTALL_SCOPES:
        console.print(
            f"[red]error:[/red] invalid --scope {scope!r} "
            f"(valid: {', '.join(_VALID_INSTALL_SCOPES)})"
        )
        raise typer.Exit(code=2)

    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(f"[red]error:[/red] no plan.json in {run_dir} — run `franklin plan` first")
        raise typer.Exit(code=1)

    plan = run.load_plan()
    plugin_root = run.output_dir / plan.plugin.name
    if not plugin_root.exists():
        console.print(
            f"[red]error:[/red] no assembled plugin at {plugin_root} — "
            "run `franklin reduce` and `franklin assemble` first"
        )
        raise typer.Exit(code=1)

    console.rule(f"[bold]franklin install[/bold] — {plan.plugin.name} ({scope})")
    console.print(f"  plugin root: {plugin_root}")
    console.print()

    if scope == "local":
        _install_local(plugin_root, plan.plugin.name, plan.plugin.version)
        return

    try:
        result = install_plugin(plugin_root, force=force)
    except InstallError as exc:
        console.print(f"[red]✗ install failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    verb = "replaced" if result.replaced else "installed"
    console.print(
        f"[green]✓[/green] {verb} [cyan]{result.plugin_name}[/cyan] "
        f"v{result.plugin_version} at {result.plugin_root}"
    )
    console.print()

    install_suffix = " --scope project" if scope == "project" else ""
    console.print("[bold]Activate in Claude Code:[/bold]")
    console.print(f"  [cyan]/plugin marketplace add[/cyan] {result.marketplace_root}")
    console.print(f"  [cyan]/plugin install[/cyan] {result.plugin_name}@franklin{install_suffix}")
    console.print("  [cyan]/reload-plugins[/cyan]")
    console.print()

    if scope == "project":
        console.print(
            "[dim]Project scope records the install in the current project's "
            ".claude/settings.json — run the commands above from a Claude Code "
            "session launched in that project.[/dim]"
        )
    else:
        console.print(
            "[dim]After the first time, re-running franklin install for any plugin "
            "only requires the second command — the marketplace is already added.[/dim]"
        )


def _install_local(plugin_root: Path, plugin_name: str, plugin_version: str) -> None:
    """Handle the --scope local branch: print only, no filesystem writes."""
    absolute = plugin_root.resolve()
    console.print(
        f"[green]✓[/green] [cyan]{plugin_name}[/cyan] v{plugin_version} "
        "is ready for session-scoped loading"
    )
    console.print(f"  plugin tree: {absolute}")
    console.print()
    console.print("[bold]Launch Claude Code with the plugin loaded for one session:[/bold]")
    console.print(f"  [cyan]claude --plugin-dir[/cyan] {absolute}")
    console.print()
    console.print(
        "[dim]Local scope is ephemeral — nothing was written to disk and the plugin "
        "is only active for the lifetime of that `claude` process. For a persistent "
        "install, re-run with --scope user or --scope project.[/dim]"
    )


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


def _run_gate_two(run_dir: Path) -> None:
    """Post-map, pre-plan gate: show what the map extracted before the Opus call.

    Loads all sidecars, prints per-chapter extraction counts, surfaces
    cross-chapter patterns (concepts appearing in 2+ chapters), and shows
    the estimated plan + reduce cost. Prompts the user to proceed or cancel.
    Non-TTY invocations auto-proceed.
    """
    import sys
    from collections import Counter

    run = RunDirectory(run_dir)
    if not run.book_json.exists():
        return  # nothing to gate

    sidecar_ids = [p.stem for p in sorted(run.chapters_dir.glob("*.json"))]
    if not sidecar_ids:
        return  # map hasn't run

    sidecars = [run.load_sidecar(cid) for cid in sidecar_ids]

    console.print()
    console.rule("[bold]Map summary — review before plan[/bold]")

    # Per-chapter extraction counts
    table = Table(show_header=True, header_style="bold")
    table.add_column("Chapter", style="cyan")
    table.add_column("Concepts", justify="right")
    table.add_column("Principles", justify="right")
    table.add_column("Rules", justify="right")
    table.add_column("Anti-pat", justify="right")
    table.add_column("Workflows", justify="right")
    table.add_column("Code", justify="right")

    totals = {"concepts": 0, "principles": 0, "rules": 0, "anti": 0, "workflows": 0, "code": 0}
    for sc in sidecars:
        nc = len(sc.concepts)
        np = len(sc.principles)
        nr = len(sc.rules)
        na = len(sc.anti_patterns)
        nw = len(sc.actionable_workflows)
        ncode = len(sc.code_examples)
        totals["concepts"] += nc
        totals["principles"] += np
        totals["rules"] += nr
        totals["anti"] += na
        totals["workflows"] += nw
        totals["code"] += ncode
        table.add_row(
            f"{sc.chapter_id}: {sc.title[:40]}",
            str(nc) if nc else "[dim]-[/dim]",
            str(np) if np else "[dim]-[/dim]",
            str(nr) if nr else "[dim]-[/dim]",
            str(na) if na else "[dim]-[/dim]",
            str(nw) if nw else "[dim]-[/dim]",
            str(ncode) if ncode else "[dim]-[/dim]",
        )
    table.add_row(
        "[bold]total[/bold]",
        f"[bold]{totals['concepts']}[/bold]",
        f"[bold]{totals['principles']}[/bold]",
        f"[bold]{totals['rules']}[/bold]",
        f"[bold]{totals['anti']}[/bold]",
        f"[bold]{totals['workflows']}[/bold]",
        f"[bold]{totals['code']}[/bold]",
    )
    console.print(table)

    # Cross-chapter themes: concepts that appear in 2+ chapters (by name, case-insensitive)
    concept_chapters: dict[str, list[str]] = {}
    for sc in sidecars:
        for c in sc.concepts:
            key = c.name.lower()
            concept_chapters.setdefault(key, []).append(sc.chapter_id)
    cross_chapter = {k: v for k, v in concept_chapters.items() if len(v) >= 2}
    if cross_chapter:
        console.print()
        console.print(
            f"  [bold]Cross-chapter concepts[/bold] ({len(cross_chapter)} spanning 2+ chapters):"
        )
        for name, chapters in sorted(cross_chapter.items(), key=lambda x: -len(x[1]))[:10]:
            console.print(f"    {name} — {', '.join(chapters)}")

    # Top anti-patterns
    anti_counts = Counter(a.name for sc in sidecars for a in sc.anti_patterns)
    if anti_counts:
        console.print()
        console.print(f"  [bold]Anti-patterns extracted:[/bold] {sum(anti_counts.values())} total")
        for name, count in anti_counts.most_common(5):
            console.print(f"    {name} ({count}x)")

    console.print()
    console.print(
        f"  [dim]{len(sidecars)} chapters mapped · "
        f"{totals['concepts']} concepts · {totals['rules']} rules · "
        f"{totals['workflows']} workflows · {totals['code']} code examples[/dim]"
    )
    console.print(
        "  [dim]Next: plan (1 Opus call) + reduce (~"
        f"{max(8, len(sidecars) // 2 + 8)} Sonnet calls)[/dim]"
    )
    console.print()

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return  # auto-proceed in non-interactive mode

    import questionary

    action = questionary.select(
        "Proceed to plan?",
        choices=[
            questionary.Choice("Proceed", value="proceed"),
            questionary.Choice("Cancel", value="cancel"),
        ],
    ).ask()

    if action is None or action == "cancel":
        console.print("[dim]cancelled[/dim]")
        raise typer.Exit(code=0)


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
    import sys

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
    import sys

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


@app.command(name="doctor")
def doctor_command(
    skip_network: bool = typer.Option(
        False,
        "--skip-network",
        help="Skip the Anthropic reachability probe",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit the check results as JSON"),
) -> None:
    """Run a preflight health check and report pass/warn/fail per item."""
    results = run_checks(skip_network=skip_network)

    if output_json:
        import json as _json

        payload = [{"name": r.name, "status": r.status.value, "detail": r.detail} for r in results]
        console.print_json(_json.dumps(payload))
        raise typer.Exit(code=1 if has_failures(results) else 0)

    console.print()
    console.rule("[bold]franklin doctor[/bold]")
    for r in results:
        icon, color = _doctor_presentation(r.status)
        console.print(f"  {icon} [bold]{r.name:<28}[/bold] [{color}]{r.detail}[/{color}]")
    console.print()

    fail_count = sum(1 for r in results if r.status == CheckStatus.FAIL)
    warn_count = sum(1 for r in results if r.status == CheckStatus.WARN)
    if fail_count:
        console.print(f"[red]✗ {fail_count} failing check(s), {warn_count} warning(s)[/red]")
        raise typer.Exit(code=1)
    if warn_count:
        console.print(f"[yellow]⚠ {warn_count} warning(s); no hard failures[/yellow]")
        return
    console.print("[green]✓ all checks passed[/green]")


def _doctor_presentation(status: CheckStatus) -> tuple[str, str]:
    return {
        CheckStatus.OK: ("[green]✓[/green]", "dim"),
        CheckStatus.WARN: ("[yellow]⚠[/yellow]", "yellow"),
        CheckStatus.FAIL: ("[red]✗[/red]", "red"),
    }[status]


@app.command(name="stats")
def stats_command(
    base: Path = typer.Option(
        Path("./runs"),
        "--base",
        "-b",
        help="Base directory to scan for runs",
    ),
) -> None:
    """Show aggregate statistics across all runs.

    Quick dashboard: total books processed, total artifacts generated,
    average grade, total cost, books by format. Read-only, no LLM calls.
    """
    summaries = list_runs(base)
    if not summaries:
        console.print(f"[dim]no runs found under {base}[/dim]")
        return

    total_books = len(summaries)
    completed = [s for s in summaries if s.last_stage == "assemble"]
    partial = [s for s in summaries if s.last_stage and s.last_stage != "assemble"]
    ingest_only = [s for s in summaries if not s.last_stage or s.last_stage == "ingest"]

    # Grades
    grades: list[float] = []
    grade_letters: dict[str, int] = {}
    total_artifacts = 0
    for s in completed:
        try:
            g = grade_run(s.path)
            grades.append(g.composite_score)
            grade_letters[g.letter] = grade_letters.get(g.letter, 0) + 1
            total_artifacts += len(g.artifact_grades)
        except Exception:
            pass

    # Costs
    total_cost = 0.0
    for s in summaries:
        run = RunDirectory(s.path)
        for e in run.load_costs():
            total_cost += float(str(e.get("cost_usd", 0)))

    # Formats
    formats: dict[str, int] = {}
    for s in summaries:
        fmt = "unknown"
        run = RunDirectory(s.path)
        if run.book_json.exists():
            try:
                book = run.load_book()
                fmt = book.source.format
            except Exception:
                pass
        formats[fmt] = formats.get(fmt, 0) + 1

    console.rule("[bold]Franklin stats[/bold]")
    console.print()
    console.print(f"  [bold]Books:[/bold]     {total_books} total")
    console.print(
        f"               {len(completed)} completed, "
        f"{len(partial)} partial, {len(ingest_only)} ingest-only"
    )
    if formats:
        fmt_str = ", ".join(f"{k}: {v}" for k, v in sorted(formats.items()))
        console.print(f"  [bold]Formats:[/bold]   {fmt_str}")
    console.print(f"  [bold]Artifacts:[/bold] {total_artifacts} generated")
    if grades:
        avg = sum(grades) / len(grades)
        console.print(f"  [bold]Avg grade:[/bold] {avg:.2f}")
        dist = ", ".join(f"{ltr}: {cnt}" for ltr, cnt in sorted(grade_letters.items()))
        console.print(f"               {dist}")
    if total_cost > 0:
        console.print(f"  [bold]Total cost:[/bold] ${total_cost:.2f}")
        if completed:
            console.print(f"  [bold]Avg/book:[/bold]  ${total_cost / len(completed):.2f}")
    console.print()


@app.command(name="costs")
def costs_command(
    base: Path = typer.Option(
        Path("./runs"),
        "--base",
        "-b",
        help="Base directory to scan for runs",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit as JSON"),
) -> None:
    """Show actual API spend across all runs.

    Reads ``costs.json`` from each run directory and prints a per-run +
    per-stage breakdown with totals. Only includes runs that have cost
    data (stages run after this feature was added).
    """
    from franklin.checkpoint import list_runs as _list_runs

    summaries = _list_runs(base)
    if not summaries:
        console.print(f"[dim]no runs found under {base}[/dim]")
        return

    all_entries: list[dict[str, object]] = []
    per_run: list[tuple[str, str | None, float]] = []

    for s in summaries:
        run = RunDirectory(s.path)
        entries = run.load_costs()
        if not entries:
            continue
        total: float = sum(float(str(e.get("cost_usd", 0))) for e in entries)
        all_entries.extend(entries)
        per_run.append((s.slug, s.title, total))

    if not per_run:
        console.print(
            "[dim]no cost data found — costs are tracked from the next run onwards[/dim]"
        )
        return

    if output_json:
        import json as _json

        console.print_json(_json.dumps(all_entries, default=str))
        return

    grand_total = sum(t for _, _, t in per_run)

    console.rule("[bold]Actual API spend[/bold]")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Run", style="cyan", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Cost (USD)", justify="right")
    for slug, title, run_cost in per_run:
        table.add_row(slug, title or "—", f"${run_cost:.2f}")
    table.add_row("[bold]total[/bold]", "", f"[bold]${grand_total:.2f}[/bold]")
    console.print(table)

    # Per-stage breakdown
    stage_totals: dict[str, float] = {}
    for e in all_entries:
        stage = str(e.get("stage", "unknown"))
        stage_totals[stage] = stage_totals.get(stage, 0.0) + float(str(e.get("cost_usd", 0)))

    if stage_totals:
        console.print()
        console.print("[bold]By stage:[/bold]")
        for stage, cost in sorted(stage_totals.items(), key=lambda x: -x[1]):
            bar_len = int(cost / grand_total * 30) if grand_total > 0 else 0
            bar = "█" * bar_len
            console.print(f"  {stage:<10} ${cost:>6.2f}  [cyan]{bar}[/cyan]")
    console.print()


@runs_app.command("list")
def runs_list_command(
    base: Path = typer.Option(
        Path("./runs"),
        "--base",
        "-b",
        help="Base directory to scan for runs",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit the run list as JSON"),
) -> None:
    """List every run directory under ./runs/ with grade and last stage."""
    summaries = list_runs(base)

    if output_json:
        import json as _json

        payload = [_summary_to_dict(s) for s in summaries]
        console.print_json(_json.dumps(payload, default=str))
        return

    if not summaries:
        console.print(f"[dim]no runs found under {base}[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Slug", style="cyan", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Ingested", style="dim")
    table.add_column("Stage")
    table.add_column("Artifacts", justify="right")
    table.add_column("Grade", justify="center")
    for s in summaries:
        table.add_row(
            s.slug,
            s.title or "[dim](unknown)[/dim]",
            s.ingested_at.strftime("%Y-%m-%d") if s.ingested_at else "—",
            _format_stage(s.last_stage),
            str(s.artifact_count) if s.artifact_count is not None else "—",
            _format_grade(s.grade_letter),
        )
    console.print(table)
    console.print(f"[dim]{len(summaries)} run(s) under {base}[/dim]")


def _summary_to_dict(s: RunSummary) -> dict[str, Any]:
    return {
        "slug": s.slug,
        "path": str(s.path),
        "title": s.title,
        "authors": s.authors,
        "ingested_at": s.ingested_at.isoformat() if s.ingested_at else None,
        "stages_done": s.stages_done,
        "last_stage": s.last_stage,
        "artifact_count": s.artifact_count,
        "grade_letter": s.grade_letter,
        "grade_score": s.grade_score,
    }


def _format_stage(last_stage: str | None) -> str:
    if last_stage is None:
        return "[dim]—[/dim]"
    if last_stage == "assemble":
        return "[green]assemble ✓[/green]"
    return f"[yellow]{last_stage}[/yellow]"


def _format_grade(letter: str | None) -> str:
    if letter is None:
        return "[dim]—[/dim]"
    color = {
        "A": "bold green",
        "A-": "bold green",
        "B+": "green",
        "B": "green",
        "B-": "yellow",
        "C+": "yellow",
        "C": "yellow",
        "C-": "yellow",
        "D": "red",
        "F": "bold red",
    }.get(letter, "white")
    return f"[{color}]{letter}[/{color}]"


@license_app.command("login")
def license_login_command(
    token: str | None = typer.Option(
        None,
        "--token",
        help="License JWT (omit to prompt interactively)",
    ),
) -> None:
    """Verify a license JWT and store it at ~/.config/franklin/license.jwt."""
    if token is None:
        token = typer.prompt("Paste your license token", hide_input=True)

    try:
        result = license_login(token)
    except LicenseError as exc:
        console.print(f"[red]✗ login failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]✓[/green] license verified for [cyan]{result.subject}[/cyan]")
    if result.plan:
        console.print(f"  plan:     {result.plan}")
    if result.features:
        console.print(f"  features: {', '.join(result.features)}")
    console.print(f"  expires:  {result.expires_at.isoformat()}")


@license_app.command("logout")
def license_logout_command() -> None:
    """Delete the stored license file."""
    removed = license_logout()
    if removed:
        console.print("[green]✓[/green] license removed")
    else:
        console.print("[dim]no license was installed[/dim]")


@license_app.command("whoami")
def license_whoami_command() -> None:
    """Show the currently installed license, if any."""
    try:
        result = license_whoami()
    except LicenseError as exc:
        console.print(f"[red]✗[/red] stored license is invalid: {exc}")
        raise typer.Exit(code=1) from exc

    if result is None:
        console.print("[dim]no license installed — run `franklin license login`[/dim]")
        return

    console.print(f"[bold]Subject:[/bold]  {result.subject}")
    if result.plan:
        console.print(f"[bold]Plan:[/bold]     {result.plan}")
    console.print(
        f"[bold]Features:[/bold] {', '.join(result.features) if result.features else '(none)'}"
    )
    console.print(f"[bold]Issued:[/bold]   {result.issued_at.isoformat()}")
    console.print(f"[bold]Expires:[/bold]  {result.expires_at.isoformat()}")
    if result.jti:
        console.print(f"[bold]JTI:[/bold]      {result.jti}")


@license_app.command("status")
def license_status_command(
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Force a phone-home to the revocation endpoint before reporting",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the LicenseStatus as JSON for support tooling",
    ),
) -> None:
    """Report operational health of the installed license.

    Never raises. Reports every failure mode (no license, corrupt file,
    expired, revoked, past hard grace) as a health level so support and
    users can diagnose gated-command failures without digging into logs.
    """
    refresh_note: str | None = None
    if refresh:
        ok = refresh_revocations()
        refresh_note = "refresh succeeded" if ok else "refresh failed (using cached state)"

    result = license_status()

    if output_json:
        import json

        payload = result.to_dict()
        if refresh_note is not None:
            payload["refresh"] = refresh_note
        console.print_json(json.dumps(payload, default=str))
        return

    _print_license_status(result, refresh_note=refresh_note)


def _print_license_status(status: LicenseStatus, *, refresh_note: str | None) -> None:
    """Render the license status as a Rich panel with color-coded health."""
    icon, color = _license_health_presentation(status.health)

    if refresh_note is not None:
        console.print(f"[dim]{refresh_note}[/dim]")

    console.print()
    console.print(f"{icon} [bold {color}]{status.health.value}[/bold {color}]")
    if status.bypass_active and status.underlying_health is not None:
        under_icon, under_color = _license_health_presentation(status.underlying_health)
        console.print(
            f"    [dim]underlying:[/dim] {under_icon} "
            f"[{under_color}]{status.underlying_health.value}[/{under_color}]"
        )
    console.print()

    if status.license is not None:
        lic = status.license
        console.print(f"[bold]Subject:[/bold]  {lic.subject}")
        if lic.plan:
            console.print(f"[bold]Plan:[/bold]     {lic.plan}")
        console.print(
            f"[bold]Features:[/bold] {', '.join(lic.features) if lic.features else '(none)'}"
        )
        console.print(
            f"[bold]Expires:[/bold]  {lic.expires_at.isoformat()}"
            + (
                f"  [dim]({_relative_days(status.days_until_expiry)})[/dim]"
                if status.days_until_expiry is not None
                else ""
            )
        )
        if lic.jti:
            console.print(f"[bold]JTI:[/bold]      {lic.jti}")

    if status.days_since_online is not None:
        console.print(
            f"[bold]Last online:[/bold] {status.days_since_online} day(s) ago "
            f"[dim](band: {status.grace_band})[/dim]"
        )
    elif status.license is not None:
        console.print("[bold]Last online:[/bold] [yellow]never[/yellow]")

    if status.detail:
        console.print(f"[bold]Detail:[/bold] [dim]{status.detail}[/dim]")

    console.print()
    console.print(f"[bold]Next step:[/bold] {status.next_step}")


def _license_health_presentation(health: LicenseHealth) -> tuple[str, str]:
    mapping: dict[LicenseHealth, tuple[str, str]] = {
        LicenseHealth.VALID: ("[green]✓[/green]", "green"),
        LicenseHealth.HARD_GRACE: ("[yellow]⚠[/yellow]", "yellow"),
        LicenseHealth.BLOCKED_EXPIRED: ("[red]✗[/red]", "red"),
        LicenseHealth.BLOCKED_REVOKED: ("[red]✗[/red]", "red"),
        LicenseHealth.BLOCKED_HARD_GRACE: ("[red]✗[/red]", "red"),
        LicenseHealth.BLOCKED_NO_ONLINE_CHECK: ("[red]✗[/red]", "red"),
        LicenseHealth.NO_LICENSE: ("[dim]⚫[/dim]", "dim"),
        LicenseHealth.CORRUPT_LICENSE: ("[red]✗[/red]", "red"),
        LicenseHealth.BYPASS_ACTIVE: ("[yellow]⚠[/yellow]", "yellow"),
    }
    return mapping.get(health, ("•", "white"))


def _relative_days(days: int) -> str:
    if days > 0:
        return f"in {days} days"
    if days == 0:
        return "today"
    return f"{-days} days ago"


if __name__ == "__main__":
    app()
