"""Franklin CLI entrypoint.

Exposes per-stage commands (ingest, map, plan, reduce, assemble) plus
a top-level `run` that chains them end-to-end.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from franklin.checkpoint import (
    RunDirectory,
    slugify,
    summarize_run,
)
from franklin.errors import FriendlyError, format_friendly_error
from franklin.estimate import RunEstimate, estimate_run
from franklin.ingest import UnsupportedFormatError, ingest_book
from franklin.license import (
    LicenseError,
    ensure_license,
)
from franklin.mapper import DEFAULT_MODEL
from franklin.planner import DEFAULT_MODEL as PLANNER_DEFAULT_MODEL
from franklin.reducer import DEFAULT_MODEL as REDUCER_DEFAULT_MODEL
from franklin.schema import (
    BookManifest,
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
    # Imported inside the function body to avoid cli ↔ commands.* circular
    # imports at module load: each commands.* submodule imports cli for its
    # app / helpers, and cli in turn needs these command functions only at
    # call time.
    from franklin.commands.operations import review_command
    from franklin.commands.publishing import (
        _validate_push_flags,
        publish_command,
        push_command,
    )

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
    from franklin.commands.stages import (
        _do_assemble_stage,
        _do_ingest_stage,
        _do_map_stage,
        _do_plan_stage,
        _do_reduce_stage,
    )

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


# Command submodules register themselves on the Typer apps above. Imported
# here, at the bottom, so the apps exist when the submodules look them up.
from franklin import commands  # noqa: E402, F401

if __name__ == "__main__":
    app()
