"""Post-pipeline operations on existing runs.

``review``, ``grade``, ``diff``, ``validate``, ``fix``, ``inspect``, ``batch``:
commands that work on run directories after the main pipeline has
assembled something. Each registers on the shared Typer ``app`` at
import time.

``_FIX_SCORE_THRESHOLD`` lives here and is re-imported by
:mod:`franklin.commands.publishing` for its "fix before publish" flow.
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path

import typer
from rich.table import Table

from franklin.checkpoint import RunDirectory
from franklin.cli import (
    _DEFAULT_REDUCE_CONCURRENCY,
    _invoke_reduce,
    _resolve_run_dir,
    app,
)
from franklin.cli import console as console
from franklin.grading import RunGrade, grade_run
from franklin.inspector import (
    ChapterInspection,
    InspectError,
    InspectReport,
    inspect_run,
    report_to_json,
)
from franklin.llm.models import REDUCE_MODEL
from franklin.review import apply_omissions, parse_omit_selection
from franklin.schema import Artifact
from franklin.services import ReduceContext, ReduceService

# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# grade
# ---------------------------------------------------------------------------


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
        console.print_json(_json.dumps(grade.to_metrics_dict(), default=str))
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


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------


_FIX_SCORE_THRESHOLD = 0.83  # below B


@app.command(name="fix")
def fix_command(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="Run directory to fix"),
    model: str = typer.Option(
        REDUCE_MODEL, "--model", help="Anthropic model ID for regeneration"
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
    # Deferred import: cli imports commands at the bottom of its module body,
    # so we can't top-level import assemble_pipeline from cli without a cycle.
    from franklin.cli import assemble_pipeline

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


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


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
    from franklin.cli import run_pipeline  # deferred — see pick.py for the pattern.

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
