"""Franklin CLI entrypoint.

Exposes per-stage commands (ingest, map, plan, reduce, assemble) plus
a top-level `run` that chains them. Only ingest is wired up in v0.1.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from franklin.checkpoint import RunDirectory, slugify
from franklin.classify import classify_chapters
from franklin.ingest import ingest_epub
from franklin.mapper import DEFAULT_MODEL, build_user_prompt, extract_chapter
from franklin.schema import BookManifest, ChapterKind, NormalizedChapter
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
