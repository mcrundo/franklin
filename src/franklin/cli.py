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
from franklin.schema import BookManifest, ChapterKind, NormalizedChapter

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


@app.command(name="run")
def run_pipeline(
    book_path: Path = typer.Argument(..., exists=True, readable=True),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    """Run the full pipeline end-to-end. [v0.1: only ingest is implemented]"""
    ingest(book_path=book_path, output=output)
    console.print("[yellow]map / plan / reduce / assemble: not yet implemented[/yellow]")


if __name__ == "__main__":
    app()
