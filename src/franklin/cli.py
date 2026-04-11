"""Franklin CLI entrypoint.

Exposes per-stage commands (ingest, map, plan, reduce, assemble) plus
a top-level `run` that chains them. Only ingest is wired up in v0.1.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from franklin.assembler import BrokenLink, validate_links, write_plugin_manifest
from franklin.checkpoint import RunDirectory, slugify
from franklin.classify import classify_chapters
from franklin.ingest import ingest_epub
from franklin.mapper import DEFAULT_MODEL, build_user_prompt, extract_chapter
from franklin.planner import DEFAULT_MODEL as PLANNER_DEFAULT_MODEL
from franklin.planner import build_user_prompt as build_plan_prompt
from franklin.planner import design_plan
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
console = Console()


def _resolve_run_dir(book_path: Path, output: Path | None) -> RunDirectory:
    if output is not None:
        return RunDirectory(output)
    slug = slugify(book_path.stem)
    return RunDirectory(Path.cwd() / "runs" / slug)


@app.command()
def ingest(
    book_path: Path = typer.Argument(..., exists=True, readable=True, help="Path to .epub"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Run directory (default: ./runs/<slug>)"
    ),
) -> None:
    """Parse an EPUB into normalized chapters and a partial book.json."""
    run = _resolve_run_dir(book_path, output)
    run.ensure()

    console.print(f"[bold]Ingesting[/bold] {book_path}")
    manifest, chapters = ingest_epub(book_path)

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


def _dry_run_prompt(
    run: RunDirectory, manifest: BookManifest, chapter: NormalizedChapter
) -> None:
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
        console.print(
            f"[red]error:[/red] no book.json in {run_dir} — run `franklin ingest` first"
        )
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
    console.print(
        f"  [dim]{input_tokens:,} input tokens / {output_tokens:,} output tokens[/dim]"
    )
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
        console.print(
            f"[red]error:[/red] no plan.json in {run_dir} — run `franklin plan` first"
        )
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
        run, plan=plan, book=manifest, sidecars=sidecars, targets=targets,
        model=model, force=force,
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
                f"[red]error:[/red] unknown artifact type {type_filter!r} "
                f"(valid: {valid})"
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
            console.print(
                f"[dim]skip[/dim] {artifact.id} — {artifact.path} already exists"
            )
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
) -> None:
    """Assemble the generated plugin tree: write plugin.json and report."""
    run = RunDirectory(run_dir)
    if not run.plan_json.exists():
        console.print(
            f"[red]error:[/red] no plan.json in {run_dir} — run `franklin plan` first"
        )
        raise typer.Exit(code=1)

    plan = run.load_plan()
    plugin_root = run.output_dir / plan.plugin.name
    if not plugin_root.exists():
        console.print(
            f"[red]error:[/red] no generated plugin at {plugin_root} — "
            "run `franklin reduce` first"
        )
        raise typer.Exit(code=1)

    console.print(f"[bold]Assembling[/bold] [cyan]{plan.plugin.name}[/cyan]")
    console.print(f"  plugin root: {plugin_root}")
    console.print()

    manifest_path = write_plugin_manifest(plugin_root, plan.plugin)
    console.print(
        f"[green]✓[/green] wrote {manifest_path.relative_to(plugin_root)}"
    )

    files = sorted(p for p in plugin_root.rglob("*") if p.is_file())
    markdown_files = [p for p in files if p.suffix == ".md"]
    console.print(
        f"  {len(files)} files total ({len(markdown_files)} markdown)"
    )

    broken_links = validate_links(plugin_root)
    if broken_links:
        _print_broken_links(plugin_root, broken_links)
    else:
        console.print("[green]✓[/green] all markdown links resolve")

    console.print()
    if broken_links:
        console.print(
            f"[yellow]⚠ assemble finished with {len(broken_links)} broken link(s)[/yellow]"
        )
    else:
        console.print(f"[green]✓[/green] assemble complete: {plugin_root}")


def _print_broken_links(plugin_root: Path, broken: list[BrokenLink]) -> None:
    console.print()
    console.print(f"[red]✗[/red] {len(broken)} broken link(s):")

    table = Table(show_header=True, header_style="bold red")
    table.add_column("Source file", style="cyan", overflow="fold")
    table.add_column("Line", justify="right")
    table.add_column("Target path", overflow="fold")
    table.add_column("Link text", overflow="fold")
    for link in broken:
        source = str(link.source_file.relative_to(plugin_root))
        table.add_row(source, str(link.line_number), link.target_path, link.link_text)
    console.print(table)


@app.command(name="run")
def run_pipeline(
    book_path: Path = typer.Argument(..., exists=True, readable=True),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    """Run the full pipeline end-to-end. [v0.1: ingest + classify only]"""
    ingest(book_path=book_path, output=output)
    console.print("[yellow]map / plan / reduce / assemble: not yet wired into run[/yellow]")


if __name__ == "__main__":
    app()
