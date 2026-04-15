"""Publishing commands: push to GitHub, interactive publish, install locally.

``push``, ``publish``, and ``install`` all take an assembled plugin
tree and move it somewhere a user can run it from. ``_validate_push_flags``
stays public so ``run_pipeline`` in ``franklin.cli`` can reuse it
when ``--push`` is threaded through the end-to-end run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from franklin.checkpoint import RunDirectory
from franklin.cli import _gate_pro_feature, app
from franklin.cli import console as console
from franklin.commands.operations import _FIX_SCORE_THRESHOLD, fix_command
from franklin.commands.stages import _print_grade_card
from franklin.grading import grade_run
from franklin.installer import InstallError, install_plugin
from franklin.llm.models import REDUCE_MODEL
from franklin.publisher import PushError, push_plugin


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
                model=REDUCE_MODEL,
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
