"""Read-only diagnostic commands — no LLM, no filesystem mutation.

``doctor``, ``stats``, ``costs``, ``runs list``, and the ``license *``
subcommands live here. Each registers itself on the shared Typer app
from :mod:`franklin.cli` at import time.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from franklin.checkpoint import RunDirectory, RunSummary, list_runs
from franklin.cli import app, console, license_app, runs_app
from franklin.doctor import CheckStatus, has_failures, run_checks
from franklin.grading import grade_run
from franklin.license import (
    LicenseError,
    LicenseHealth,
    LicenseStatus,
    refresh_revocations,
)
from franklin.license import login as license_login
from franklin.license import logout as license_logout
from franklin.license import status as license_status
from franklin.license import whoami as license_whoami

# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# costs
# ---------------------------------------------------------------------------


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
    summaries = list_runs(base)
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


# ---------------------------------------------------------------------------
# runs list
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# license *
# ---------------------------------------------------------------------------


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
        payload = result.to_dict()
        if refresh_note is not None:
            payload["refresh"] = refresh_note
        console.print_json(_json.dumps(payload, default=str))
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
