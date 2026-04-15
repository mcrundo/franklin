"""Pipeline stage commands + their Rich renderers.

Holds the five per-stage Typer commands (``ingest``, ``map``, ``plan``,
``reduce``, ``assemble``), the shared ``_do_<stage>_stage`` helpers,
and the ``_StageRenderer`` family that translates ProgressEvents from
each service into the Rich progress bars users see.

The Typer command for each stage is a one-liner delegating to its
``_do_*_stage`` helper; ``run_pipeline`` in ``franklin.cli`` calls the
same helpers directly so the pipeline composes services without
dispatching through Typer.
"""

from __future__ import annotations

from pathlib import Path

import typer
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
from franklin.checkpoint import RunDirectory
from franklin.cli import _maybe_confirm_metadata, _resolve_run_dir, app
from franklin.cli import console as console
from franklin.grading import RunGrade
from franklin.ingest import UnsupportedFormatError
from franklin.mapper import DEFAULT_MODEL, build_user_prompt
from franklin.planner import DEFAULT_MODEL as PLANNER_DEFAULT_MODEL
from franklin.reducer import DEFAULT_MODEL as REDUCER_DEFAULT_MODEL
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


# ---------------------------------------------------------------------------
# Stage renderers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# map
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# reduce
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------


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

    Kept as a standalone helper so the ``run`` orchestrator can reuse
    it when composing services.
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
