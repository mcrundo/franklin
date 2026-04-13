"""Push an assembled Claude Code plugin tree to a GitHub repository.

Two backends in priority order:

1. **gh CLI** — if `gh` is on PATH, use it for repo existence checks, repo
   creation, and PR creation. gh handles auth transparently via its own
   credential store, so no token management is needed.
2. **REST API fallback** — if gh isn't installed, fall back to GitHub's
   REST API with a Personal Access Token read from the `GITHUB_TOKEN`
   environment variable. Requires `repo` scope to create repos and push.

Git operations (init, add, commit, push) always use the local git binary
via subprocess — both backends share that path.

Keychain storage for GitHub credentials is deferred; env vars are the
standard for dev-facing CLIs in this space and users who want stronger
storage can use 1Password, direnv, or similar external wrappers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


class PushError(RuntimeError):
    """Raised when push preparation or execution fails."""


@dataclass(frozen=True)
class PushResult:
    """Outcome of a successful push_plugin call."""

    repo_url: str
    branch: str
    created_repo: bool
    pr_url: str | None
    backend: str


def push_plugin(
    plugin_root: Path,
    *,
    repo: str,
    branch: str = "main",
    create_pr: bool = False,
    public: bool = False,
    commit_message: str,
) -> PushResult:
    """Push ``plugin_root`` to ``github.com/<repo>``.

    Creates the repository if it does not exist (private by default,
    override with ``public=True``). Produces one commit per push with
    ``commit_message``. When ``create_pr`` is true and ``branch`` is not
    ``main``, also opens a pull request against ``main``.
    """
    owner, name = _parse_repo(repo)
    if create_pr and branch == "main":
        raise PushError("--pr requires --branch (cannot open a PR against main from main)")
    if not plugin_root.is_dir():
        raise PushError(f"plugin root does not exist: {plugin_root}")

    backend = _detect_backend()

    created_repo = False
    if not _repo_exists(owner, name, backend):
        _create_repo(owner, name, private=not public, backend=backend)
        created_repo = True

    _stage_git(plugin_root, branch=branch, commit_message=commit_message)

    remote_url = _remote_url(owner, name, backend)
    _push_branch(plugin_root, remote_url, branch)

    pr_url: str | None = None
    if create_pr:
        pr_url = _create_pr(owner, name, branch, backend, commit_message)

    return PushResult(
        repo_url=f"https://github.com/{owner}/{name}",
        branch=branch,
        created_repo=created_repo,
        pr_url=pr_url,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# Repo string parsing
# ---------------------------------------------------------------------------


def _sanitize_stderr(stderr: str) -> str:
    """Strip credentials and tokens from subprocess stderr before logging."""
    cleaned = stderr.strip()
    # Remove anything that looks like a token or credential
    cleaned = re.sub(r"(ghp_|gho_|github_pat_)[A-Za-z0-9_]+", "***", cleaned)
    cleaned = re.sub(
        r"(token|password|secret|credential)s?\s*[:=]\s*\S+",
        r"\1: ***",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _parse_repo(repo: str) -> tuple[str, str]:
    if repo.count("/") != 1:
        raise PushError(f"--repo must be of the form owner/name (got {repo!r})")
    owner, name = repo.split("/", 1)
    if not owner or not name:
        raise PushError(f"--repo must be of the form owner/name (got {repo!r})")
    if not _REPO_NAME_RE.match(owner) or not _REPO_NAME_RE.match(name):
        raise PushError(
            f"--repo owner and name must be alphanumeric with hyphens/dots/underscores "
            f"(got {repo!r})"
        )
    return owner, name


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def _detect_backend() -> str:
    if shutil.which("gh") is not None:
        return "gh"
    if os.environ.get("GITHUB_TOKEN", "").strip():
        return "rest"
    raise PushError(
        "no supported backend available: install the gh CLI "
        "(https://cli.github.com) or set GITHUB_TOKEN in your environment"
    )


# ---------------------------------------------------------------------------
# Repo existence and creation
# ---------------------------------------------------------------------------


def _repo_exists(owner: str, name: str, backend: str) -> bool:
    if backend == "gh":
        result = subprocess.run(
            ["gh", "repo", "view", f"{owner}/{name}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    try:
        _github_request("GET", f"/repos/{owner}/{name}")
    except PushError as exc:
        if "404" in str(exc):
            return False
        raise
    return True


def _create_repo(owner: str, name: str, *, private: bool, backend: str) -> None:
    if backend == "gh":
        args = [
            "gh",
            "repo",
            "create",
            f"{owner}/{name}",
            "--private" if private else "--public",
        ]
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise PushError(f"gh repo create failed: {_sanitize_stderr(result.stderr)}")
        return

    _github_request(
        "POST",
        "/user/repos",
        body={"name": name, "private": private, "auto_init": False},
    )


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------


def _stage_git(plugin_root: Path, *, branch: str, commit_message: str) -> None:
    git_dir = plugin_root / ".git"

    if not git_dir.exists():
        _git(plugin_root, "init", "-b", branch)
    else:
        _git(plugin_root, "checkout", "-B", branch)

    _git(plugin_root, "add", "-A")

    status = _git(plugin_root, "status", "--porcelain", capture=True)
    if not status.strip():
        head = _git(plugin_root, "log", "--oneline", "-1", capture=True, check=False)
        if head.strip():
            return
        raise PushError("plugin tree is empty — nothing to commit")

    _git(
        plugin_root,
        "-c",
        "user.name=franklin",
        "-c",
        "user.email=franklin@localhost",
        "commit",
        "-m",
        commit_message,
    )


def _push_branch(plugin_root: Path, remote_url: str, branch: str) -> None:
    existing = _git(
        plugin_root,
        "remote",
        "get-url",
        "origin",
        capture=True,
        check=False,
    )
    if existing.strip():
        _git(plugin_root, "remote", "set-url", "origin", remote_url)
    else:
        _git(plugin_root, "remote", "add", "origin", remote_url)

    _git(plugin_root, "push", "-u", "origin", branch)


def _remote_url(owner: str, name: str, backend: str) -> str:
    if backend == "gh":
        return f"https://github.com/{owner}/{name}.git"
    token = os.environ["GITHUB_TOKEN"].strip()
    return f"https://x-access-token:{token}@github.com/{owner}/{name}.git"


def _git(
    cwd: Path,
    *args: str,
    capture: bool = False,
    check: bool = True,
) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise PushError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{_sanitize_stderr(result.stderr) or result.stdout.strip()}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Pull request creation
# ---------------------------------------------------------------------------


def _create_pr(
    owner: str,
    name: str,
    branch: str,
    backend: str,
    title: str,
) -> str:
    body = f"Generated by `franklin push` from the `{branch}` branch."

    if backend == "gh":
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                f"{owner}/{name}",
                "--head",
                branch,
                "--base",
                "main",
                "--title",
                title,
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise PushError(f"gh pr create failed: {_sanitize_stderr(result.stderr)}")
        return result.stdout.strip()

    response = _github_request(
        "POST",
        f"/repos/{owner}/{name}/pulls",
        body={
            "title": title,
            "head": branch,
            "base": "main",
            "body": body,
        },
    )
    return str(response.get("html_url", ""))


# ---------------------------------------------------------------------------
# REST helper
# ---------------------------------------------------------------------------


def _github_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise PushError("GITHUB_TOKEN not set in environment")

    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with request.urlopen(req) as response:
            payload = response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode(errors="replace") if exc.fp else ""
        raise PushError(
            f"GitHub API {method} {path} returned {exc.code}: {detail.strip()}"
        ) from exc
    except error.URLError as exc:
        raise PushError(f"GitHub API request failed: {exc.reason}") from exc

    if not payload:
        return {}
    result: Any = json.loads(payload)
    if not isinstance(result, dict):
        return {}
    return result
