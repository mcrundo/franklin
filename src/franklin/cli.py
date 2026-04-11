"""Franklin CLI entrypoint.

Exposes per-stage commands (ingest, map, plan, reduce, assemble) plus
a top-level `run` that chains them end-to-end.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from franklin.assembler import (
    BrokenLink,
    FrontmatterIssue,
    TemplateLeak,
    find_template_leaks,
    package_plugin,
    validate_frontmatter,
    validate_links,
    write_plugin_manifest,
)
from franklin.checkpoint import (
    RunDirectory,
    RunSummary,
    list_runs,
    slugify,
    summarize_run,
)
from franklin.classify import classify_chapters
from franklin.doctor import CheckStatus, has_failures, run_checks
from franklin.errors import FriendlyError, format_friendly_error
from franklin.estimate import RunEstimate, estimate_run
from franklin.grading import RunGrade, grade_run, write_metrics
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
from franklin.mapper import DEFAULT_MODEL, build_user_prompt, extract_chapter
from franklin.picker import BookCandidate, discover_books
from franklin.planner import DEFAULT_MODEL as PLANNER_DEFAULT_MODEL
from franklin.planner import build_user_prompt as build_plan_prompt
from franklin.planner import design_plan
from franklin.publisher import PushError, push_plugin
from franklin.reducer import DEFAULT_MODEL as REDUCER_DEFAULT_MODEL
from franklin.reducer import generate_artifact
from franklin.schema import (
    Artifact,
    ArtifactType,
    BookManifest,
    ChapterKind,
    ChapterSidecar,
    NormalizedChapter,
    PlanManifest,
)
from franklin.secrets import MissingApiKeyError, ensure_anthropic_api_key

app = typer.Typer(
    name="franklin",
    help="Turn technical books into Claude Code plugins.",
    no_args_is_help=True,
)
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


def _gate_pro_feature(feature: str, command: str) -> None:
    """Check the license for a premium command, or exit with a friendly error.

    Calls ensure_license(feature=...) and translates any LicenseError into
    a multi-line, ANSI-rendered explanation the user can act on. Never
    lets a stack trace reach stderr on license failure — the license
    module's messages go into the body of the panel, nothing else.
    """
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
    slug = slugify(book_path.stem)
    return RunDirectory(Path.cwd() / "runs" / slug)


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
        f"[bold]${result.total_cost_usd:,.2f}[/bold]",
    )
    console.print(table)
    console.print()
    console.print(
        "[dim]Estimates lean pessimistic; actual runs should land at or below "
        "this figure. Prompt caching can drive real cost further down.[/dim]"
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
    run = _resolve_run_dir(book_path, output)
    run.ensure()

    is_pdf = book_path.suffix.lower() == ".pdf"
    if is_pdf and not yes_i_know_pdfs:
        _print_pdf_warning()

    if clean and not is_pdf:
        console.print(
            "[dim]--clean is a no-op on EPUBs (they're already structurally clean)[/dim]"
        )
        clean = False

    console.print(f"[bold]Ingesting[/bold] {book_path}")
    try:
        manifest, chapters = ingest_book(book_path)
    except UnsupportedFormatError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _maybe_confirm_metadata(manifest, skip=yes)

    if clean:
        chapters = _run_cleanup_pass(chapters, concurrency=clean_concurrency)
        # Rebuild structure totals from the cleaned chapters so book.json reflects
        # the post-cleanup word counts.
        manifest.structure.total_words = sum(c.word_count for c in chapters)
        by_id = {e.id: e for e in manifest.structure.toc}
        for chapter in chapters:
            entry = by_id.get(chapter.chapter_id)
            if entry is not None:
                entry.word_count = chapter.word_count

    classifications = classify_chapters(chapters)
    for toc_entry in manifest.structure.toc:
        result = classifications[toc_entry.id]
        toc_entry.kind = result.kind
        toc_entry.kind_confidence = result.confidence
        toc_entry.kind_reason = result.reason

    run.save_book(manifest)
    for chapter in chapters:
        run.save_raw_chapter(chapter)

    _print_ingest_summary(run, manifest, chapters)


def _run_cleanup_pass(
    chapters: list[NormalizedChapter], *, concurrency: int
) -> list[NormalizedChapter]:
    """Invoke the Tier 4 LLM cleanup pass via the async pipeline.

    Drives ``clean_chapters_async`` via ``asyncio.run`` with a bounded
    semaphore so 29 chapters complete in roughly ``total / concurrency``
    waves of ~1.5-2 min each, instead of ~50 min sequential.
    """
    import asyncio

    from franklin.ingest.cleanup import clean_chapters_async

    try:
        ensure_anthropic_api_key()
    except MissingApiKeyError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    estimate = len(chapters) * 0.08  # rough per-chapter cleanup cost estimate in USD
    console.print()
    console.rule("[bold]Tier 4 cleanup[/bold]")
    console.print(f"  about to send {len(chapters)} chapters to Claude for mechanical cleanup")
    console.print(f"  concurrency: [cyan]{concurrency}[/cyan] in flight at once")
    console.print(
        f"  estimated cost: [yellow]~${estimate:.2f}[/yellow] total "
        "(actual will vary with chapter length)"
    )
    console.print()

    total_count = len(chapters)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]Cleaning[/bold]"),
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

    with progress:
        task_id = progress.add_task("cleanup", total=total_count, last="starting…")

        def on_progress(chapter: NormalizedChapter) -> None:
            progress.update(
                task_id,
                advance=1,
                last=f"✓ {chapter.chapter_id}",
            )

        def on_failure(chapter: NormalizedChapter, _exc: Exception) -> None:
            progress.update(
                task_id,
                advance=1,
                last=f"⚠ {chapter.chapter_id} failed",
            )

        cleaned, total_in, total_out, failed_ids = asyncio.run(
            clean_chapters_async(
                chapters,
                concurrency=concurrency,
                on_progress=on_progress,
                on_failure=on_failure,
            )
        )

    console.print()
    ok_count = len(cleaned) - len(failed_ids)
    actual_cost = (total_in / 1_000_000) * 3.0 + (total_out / 1_000_000) * 15.0
    console.print(
        f"[green]✓[/green] cleanup complete: {ok_count}/{len(cleaned)} chapters cleaned "
        f"[dim]({total_in:,} in / {total_out:,} out tokens · "
        f"${actual_cost:.2f})[/dim]"
    )
    if failed_ids:
        console.print(
            f"  [yellow]{len(failed_ids)} failures:[/yellow] "
            f"{', '.join(failed_ids)} — kept Tier 2 output for these"
        )
    return cleaned


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
) -> None:
    """Run the map stage: per-chapter structured extraction via the LLM."""
    run = RunDirectory(run_dir)
    if not run.book_json.exists():
        console.print(f"[red]error:[/red] no book.json in {run_dir} — run `franklin ingest` first")
        raise typer.Exit(code=1)

    manifest = run.load_book()
    targets = _select_targets(run, manifest, chapter)

    if not targets:
        console.print("[yellow]no chapters to extract[/yellow]")
        raise typer.Exit(code=0)

    if dry_run:
        _dry_run_prompt(run, manifest, targets[0])
        return

    try:
        ensure_anthropic_api_key()
    except MissingApiKeyError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _extract_all(run, manifest, targets, model=model, force=force)


def _select_targets(
    run: RunDirectory, manifest: BookManifest, chapter_id: str | None
) -> list[NormalizedChapter]:
    """Pick which chapters to extract.

    Without --chapter, returns every CONTENT or INTRODUCTION chapter. With
    --chapter, returns that single chapter regardless of classification (so
    users can iterate on prompts against borderline chapters if they want).
    """
    if chapter_id is not None:
        raw_path = run.raw_chapter_path(chapter_id)
        if not raw_path.exists():
            console.print(f"[red]error:[/red] chapter {chapter_id} not found in {run.raw_dir}")
            raise typer.Exit(code=1)
        return [run.load_raw_chapter(chapter_id)]

    content_ids = [
        entry.id
        for entry in manifest.structure.toc
        if entry.kind in (ChapterKind.CONTENT, ChapterKind.INTRODUCTION)
    ]
    return [run.load_raw_chapter(cid) for cid in content_ids]


def _dry_run_prompt(run: RunDirectory, manifest: BookManifest, chapter: NormalizedChapter) -> None:
    prompt = build_user_prompt(manifest, chapter)
    console.print(f"[bold]Dry run[/bold] — prompt for {chapter.chapter_id} ({chapter.title})")
    console.print(f"  run dir: {run.root}")
    console.print(f"  chars:   {len(prompt):,}")
    console.print(f"  approx tokens: {len(prompt) // 4:,}")
    console.print()
    console.print(prompt)


def _extract_all(
    run: RunDirectory,
    manifest: BookManifest,
    targets: list[NormalizedChapter],
    *,
    model: str,
    force: bool,
) -> None:
    total_in = 0
    total_out = 0
    skipped = 0

    for chapter in targets:
        sidecar_path = run.sidecar_path(chapter.chapter_id)
        if sidecar_path.exists() and not force:
            console.print(
                f"[dim]skip[/dim] {chapter.chapter_id} — sidecar exists (use --force to re-run)"
            )
            skipped += 1
            continue

        console.print(
            f"[bold]extract[/bold] {chapter.chapter_id} "
            f"([cyan]{chapter.title}[/cyan], {chapter.word_count:,} words)"
        )
        sidecar, in_toks, out_toks = extract_chapter(manifest, chapter, model=model)
        run.save_sidecar(sidecar)
        total_in += in_toks
        total_out += out_toks
        console.print(
            f"  [green]✓[/green] {len(sidecar.concepts)} concepts, "
            f"{len(sidecar.principles)} principles, "
            f"{len(sidecar.rules)} rules, "
            f"{len(sidecar.anti_patterns)} anti-patterns, "
            f"{len(sidecar.code_examples)} code examples "
            f"([dim]{in_toks:,} in / {out_toks:,} out[/dim])"
        )

    console.print()
    console.print(
        f"[green]✓[/green] map stage complete: "
        f"{len(targets) - skipped} extracted, {skipped} skipped, "
        f"[dim]{total_in:,} input tokens / {total_out:,} output tokens[/dim]"
    )


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
    run = RunDirectory(run_dir)
    if not run.book_json.exists():
        console.print(f"[red]error:[/red] no book.json in {run_dir} — run `franklin ingest` first")
        raise typer.Exit(code=1)

    manifest = run.load_book()
    sidecar_ids = [p.stem for p in sorted(run.chapters_dir.glob("*.json"))]
    if not sidecar_ids:
        console.print(
            f"[red]error:[/red] no sidecars in {run.chapters_dir} — run `franklin map` first"
        )
        raise typer.Exit(code=1)

    sidecars = [run.load_sidecar(cid) for cid in sidecar_ids]

    if run.plan_json.exists() and not force:
        console.print(
            f"[yellow]plan.json already exists at {run.plan_json}[/yellow]\n"
            "  use --force to regenerate, or open it directly to edit"
        )
        raise typer.Exit(code=1)

    if dry_run:
        prompt = build_plan_prompt(manifest, sidecars)
        console.print("[bold]Dry run[/bold] — plan prompt")
        console.print(f"  chars: {len(prompt):,}")
        console.print(f"  approx tokens: {len(prompt) // 4:,}")
        console.print(f"  sidecars: {len(sidecars)}")
        console.print()
        console.print(prompt)
        return

    try:
        ensure_anthropic_api_key()
    except MissingApiKeyError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[bold]Designing plugin[/bold] for [cyan]{manifest.metadata.title}[/cyan] "
        f"from {len(sidecars)} sidecars using [dim]{model}[/dim]"
    )
    plan, in_toks, out_toks = design_plan(manifest, sidecars, model=model)
    run.save_plan(plan)

    _print_plan_summary(run, plan, in_toks, out_toks)


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
) -> None:
    """Generate each artifact file from the plan using its feeds_from slice."""
    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(f"[red]error:[/red] no plan.json in {run_dir} — run `franklin plan` first")
        raise typer.Exit(code=1)

    manifest = run.load_book()
    plan = run.load_plan()
    sidecar_ids = [p.stem for p in sorted(run.chapters_dir.glob("*.json"))]
    if not sidecar_ids:
        console.print(
            f"[red]error:[/red] no sidecars in {run.chapters_dir} — run `franklin map` first"
        )
        raise typer.Exit(code=1)
    sidecars = {cid: run.load_sidecar(cid) for cid in sidecar_ids}

    targets = _select_artifacts(plan, artifact, type_filter)
    if not targets:
        console.print("[yellow]no artifacts to generate[/yellow]")
        raise typer.Exit(code=0)

    try:
        ensure_anthropic_api_key()
    except MissingApiKeyError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _generate_artifacts(
        run,
        plan=plan,
        book=manifest,
        sidecars=sidecars,
        targets=targets,
        model=model,
        force=force,
    )


def _select_artifacts(
    plan: PlanManifest,
    artifact_id: str | None,
    type_filter: str | None,
) -> list[Artifact]:
    if artifact_id is not None:
        for art in plan.artifacts:
            if art.id == artifact_id:
                return [art]
        console.print(
            f"[red]error:[/red] no artifact with id {artifact_id!r} in plan "
            f"(available: {', '.join(a.id for a in plan.artifacts)})"
        )
        raise typer.Exit(code=1)

    if type_filter is not None:
        try:
            kind = ArtifactType(type_filter)
        except ValueError:
            valid = ", ".join(t.value for t in ArtifactType)
            console.print(
                f"[red]error:[/red] unknown artifact type {type_filter!r} (valid: {valid})"
            )
            raise typer.Exit(code=1) from None
        return [a for a in plan.artifacts if a.type == kind]

    return list(plan.artifacts)


def _generate_artifacts(
    run: RunDirectory,
    *,
    plan: PlanManifest,
    book: BookManifest,
    sidecars: dict[str, ChapterSidecar],
    targets: list[Artifact],
    model: str,
    force: bool,
) -> None:
    output_root = run.output_dir / plan.plugin.name
    output_root.mkdir(parents=True, exist_ok=True)

    console.print(
        f"[bold]Generating[/bold] {len(targets)} artifacts for "
        f"[cyan]{plan.plugin.name}[/cyan] using [dim]{model}[/dim]"
    )
    console.print(f"  output: {output_root}")
    console.print()

    totals = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "generated": 0,
        "skipped": 0,
        "failed": 0,
    }

    for artifact in targets:
        out_path = output_root / artifact.path
        if out_path.exists() and not force:
            console.print(f"[dim]skip[/dim] {artifact.id} — {artifact.path} already exists")
            totals["skipped"] += 1
            continue

        console.print(
            f"[bold]{artifact.type.value}[/bold] {artifact.id} → [cyan]{artifact.path}[/cyan]"
        )
        try:
            result = generate_artifact(
                artifact,
                plan=plan,
                book=book,
                sidecars=sidecars,
                model=model,
            )
        except Exception as exc:
            console.print(f"  [red]✗ failed[/red]: {exc}")
            totals["failed"] += 1
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            result.content if result.content.endswith("\n") else result.content + "\n"
        )

        totals["input"] += result.input_tokens
        totals["output"] += result.output_tokens
        totals["cache_read"] += result.cache_read_tokens
        totals["cache_creation"] += result.cache_creation_tokens
        totals["generated"] += 1

        cache_note = ""
        if result.cache_read_tokens:
            cache_note = f", [green]{result.cache_read_tokens:,} cached[/green]"
        console.print(
            f"  [green]✓[/green] {len(result.content):,} chars "
            f"([dim]{result.input_tokens:,} in / {result.output_tokens:,} out{cache_note}[/dim])"
        )

    console.print()
    console.print(
        f"[green]✓[/green] reduce stage complete: "
        f"{totals['generated']} generated, {totals['skipped']} skipped, "
        f"{totals['failed']} failed"
    )
    console.print(
        f"  [dim]{totals['input']:,} input / {totals['output']:,} output / "
        f"{totals['cache_read']:,} cache-read / "
        f"{totals['cache_creation']:,} cache-write tokens[/dim]"
    )
    console.print(f"  plugin tree: [cyan]{output_root}[/cyan]")


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
    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(f"[red]error:[/red] no plan.json in {run_dir} — run `franklin plan` first")
        raise typer.Exit(code=1)

    plan = run.load_plan()
    plugin_root = run.output_dir / plan.plugin.name
    if not plugin_root.exists():
        console.print(
            f"[red]error:[/red] no generated plugin at {plugin_root} — run `franklin reduce` first"
        )
        raise typer.Exit(code=1)

    console.print(f"[bold]Assembling[/bold] [cyan]{plan.plugin.name}[/cyan]")
    console.print(f"  plugin root: {plugin_root}")
    console.print()

    manifest_path = write_plugin_manifest(plugin_root, plan.plugin)
    console.print(f"[green]✓[/green] wrote {manifest_path.relative_to(plugin_root)}")

    files = sorted(p for p in plugin_root.rglob("*") if p.is_file())
    markdown_files = [p for p in files if p.suffix == ".md"]
    console.print(f"  {len(files)} files total ({len(markdown_files)} markdown)")

    broken_links = validate_links(plugin_root)
    template_leaks = find_template_leaks(plugin_root)
    frontmatter_issues = validate_frontmatter(plugin_root)

    if broken_links:
        _print_broken_links(plugin_root, broken_links)
    else:
        console.print("[green]✓[/green] all markdown links resolve")

    if template_leaks:
        _print_template_leaks(plugin_root, template_leaks)
    else:
        console.print("[green]✓[/green] no unfilled template placeholders")

    if frontmatter_issues:
        _print_frontmatter_issues(plugin_root, frontmatter_issues)
    else:
        console.print("[green]✓[/green] all frontmatter blocks are valid")

    console.print()
    issue_count = len(broken_links) + len(template_leaks) + len(frontmatter_issues)
    if issue_count:
        console.print(f"[yellow]⚠ assemble finished with {issue_count} issue(s)[/yellow]")
    else:
        console.print(f"[green]✓[/green] assemble complete: {plugin_root}")

    grade = grade_run(run_dir)
    metrics_path = write_metrics(run_dir, grade)
    console.print()
    _print_grade_card(grade, plan_name=plan.plugin.name)
    console.print(f"  [dim]metrics: {metrics_path}[/dim]")

    if zip_archive:
        archive_path = run.output_dir / f"{plan.plugin.name}.zip"
        package_plugin(plugin_root, archive_path)
        size_kb = archive_path.stat().st_size / 1024
        console.print(
            f"[green]✓[/green] packaged [cyan]{archive_path.name}[/cyan] "
            f"({size_kb:,.1f} KB) at {archive_path}"
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
    clean: bool = typer.Option(
        False,
        "--clean",
        help="Include Tier 4 cleanup in the cost estimate (has no effect without --estimate)",
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

    console.rule(f"[bold]franklin run[/bold] — {book_path.name}")
    console.print(f"  run directory: {run.root}")
    if force:
        console.print("  [yellow]--force[/yellow]: re-running existing stages")
    if push:
        console.print(f"  [yellow]--push[/yellow]: publish to {repo} on branch {branch}")
    console.print()

    stages: list[tuple[str, Callable[[], None]]] = [
        (
            "ingest",
            lambda: ingest(
                book_path=book_path,
                output=run.root,
                yes_i_know_pdfs=False,
                clean=False,
                clean_concurrency=8,
                yes=yes,
            ),
        ),
        (
            "map",
            lambda: map_chapters(
                run_dir=run.root,
                chapter=None,
                model=DEFAULT_MODEL,
                dry_run=False,
                force=force,
            ),
        ),
        (
            "plan",
            lambda: plan_pipeline(
                run_dir=run.root,
                model=PLANNER_DEFAULT_MODEL,
                dry_run=False,
                force=force,
            ),
        ),
        (
            "reduce",
            lambda: reduce_pipeline(
                run_dir=run.root,
                artifact=None,
                type_filter=None,
                model=REDUCER_DEFAULT_MODEL,
                force=force,
            ),
        ),
        ("assemble", lambda: assemble_pipeline(run_dir=run.root, zip_archive=False)),
    ]
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

        console.rule(f"[bold cyan]{name}[/bold cyan]")
        try:
            fn()
        except typer.Exit as exc:
            if exc.exit_code:
                console.print(f"[red]✗ {name} stage failed (exit code {exc.exit_code})[/red]")
                raise typer.Exit(code=exc.exit_code) from exc
            # exit_code 0 is a graceful "nothing to do" — continue to next stage.
        except Exception as exc:
            friendly = format_friendly_error(exc)
            _print_friendly_error(friendly, stage=name)
            raise typer.Exit(code=friendly.exit_code) from exc
        console.print()

    console.rule("[bold green]pipeline complete[/bold green]")
    console.print(f"[green]✓[/green] {run.root}")


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
    search_dir: Path = typer.Option(
        Path.home() / "Downloads",
        "--dir",
        "-d",
        help="Directory to scan for .epub and .pdf files",
    ),
    runs_base: Path = typer.Option(
        Path("./runs"),
        "--runs-base",
        help="Existing runs directory to cross-reference",
    ),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Walk subdirectories"),
    limit: int = typer.Option(
        100, "--limit", help="Maximum number of candidates to display", min=1, max=500
    ),
) -> None:
    """Interactive picker for .epub/.pdf files with run-state overlay.

    Scans the given directory (default ~/Downloads) for book files,
    annotates each with whether a matching run already exists, and
    prompts for a selection. Picking a file launches ``franklin run``
    on it with the default options.
    """
    candidates = discover_books(
        search_dir, runs_base=runs_base, recursive=recursive, max_results=limit
    )
    if not candidates:
        console.print(f"[dim]no .epub or .pdf files found under {search_dir}[/dim]")
        return

    console.print()
    console.rule(f"[bold]franklin pick[/bold] — {search_dir}")
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("File", overflow="fold", style="cyan")
    table.add_column("Type", justify="center", style="dim")
    table.add_column("Size", justify="right", style="dim")
    table.add_column("Run state")
    for idx, c in enumerate(candidates, start=1):
        table.add_row(
            str(idx),
            c.display_name,
            c.extension,
            _format_size(c.size_bytes),
            _format_run_state(c),
        )
    console.print(table)
    console.print(f"[dim]{len(candidates)} candidate(s) shown[/dim]")
    console.print()

    choice = typer.prompt("Pick a number to run it (or 0 to cancel)", default=0, type=int)
    if choice == 0:
        console.print("[dim]cancelled[/dim]")
        return
    if choice < 1 or choice > len(candidates):
        console.print(f"[red]invalid selection {choice}[/red]")
        raise typer.Exit(code=1)

    picked = candidates[choice - 1]
    console.print()
    console.print(f"[green]→[/green] launching franklin run on [cyan]{picked.path}[/cyan]")
    console.print()
    run_pipeline(
        book_path=picked.path,
        output=None,
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
