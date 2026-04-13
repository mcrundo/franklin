"""Tests for franklin.publisher (franklin push)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from franklin.publisher import (
    PushError,
    PushResult,
    _detect_backend,
    _parse_repo,
    _remote_url,
    push_plugin,
)


def _make_plugin_tree(tmp_path: Path, name: str = "p") -> Path:
    """Create a minimal assembled-plugin tree with plugin.json + README."""
    plugin_root = tmp_path / name
    plugin_root.mkdir()
    (plugin_root / ".claude-plugin").mkdir()
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "0.1.0", "description": "test plugin"})
    )
    (plugin_root / "SKILL.md").write_text("# Skill\n")
    (plugin_root / "README.md").write_text("# plugin readme\n")
    return plugin_root


# ---------------------------------------------------------------------------
# Repo parsing
# ---------------------------------------------------------------------------


def test_parse_repo_accepts_owner_name() -> None:
    assert _parse_repo("palkan/skills") == ("palkan", "skills")


@pytest.mark.parametrize(
    "value",
    ["", "single", "a/b/c", "/name", "owner/", "owner//name"],
)
def test_parse_repo_rejects_invalid(value: str) -> None:
    with pytest.raises(PushError, match="owner/name"):
        _parse_repo(value)


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def test_detect_backend_prefers_gh_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xxx")
    with patch("franklin.publisher.shutil.which", return_value="/usr/local/bin/gh"):
        assert _detect_backend() == "gh"


def test_detect_backend_falls_back_to_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xxx")
    with patch("franklin.publisher.shutil.which", return_value=None):
        assert _detect_backend() == "rest"


def test_detect_backend_raises_when_neither_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with (
        patch("franklin.publisher.shutil.which", return_value=None),
        pytest.raises(PushError, match="no supported backend"),
    ):
        _detect_backend()


# ---------------------------------------------------------------------------
# Remote URL construction
# ---------------------------------------------------------------------------


def test_remote_url_gh_uses_plain_https() -> None:
    assert _remote_url("palkan", "skills", "gh") == "https://github.com/palkan/skills.git"


def test_remote_url_rest_embeds_token_for_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    assert (
        _remote_url("palkan", "skills", "rest")
        == "https://x-access-token:ghp_secret@github.com/palkan/skills.git"
    )


# ---------------------------------------------------------------------------
# push_plugin end-to-end with mocked subprocess
# ---------------------------------------------------------------------------


def _git_subcommand(args: list[str]) -> str | None:
    """Return the git subcommand name, stepping over any leading -c options."""
    if not args or args[0] != "git":
        return None
    i = 1
    while i + 1 < len(args) and args[i] == "-c":
        i += 2
    return args[i] if i < len(args) else None


def _make_fake_run(
    command_log: list[list[str]],
    *,
    repo_exists: bool = True,
    pr_stdout: str = "https://github.com/palkan/skills/pull/7\n",
    create_repo_exit: int = 0,
    create_repo_stderr: str = "",
) -> Any:
    def fake_run(
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        command_log.append(list(args))

        if args[:3] == ["gh", "repo", "view"]:
            code = 0 if repo_exists else 1
            return subprocess.CompletedProcess(args, code, stdout="", stderr="")

        if args[:3] == ["gh", "repo", "create"]:
            return subprocess.CompletedProcess(
                args, create_repo_exit, stdout="", stderr=create_repo_stderr
            )

        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(args, 0, stdout=pr_stdout, stderr="")

        sub = _git_subcommand(args)
        if sub == "status":
            return subprocess.CompletedProcess(args, 0, stdout="?? SKILL.md\n", stderr="")
        if sub == "remote" and len(args) >= 3 and args[-2] == "get-url":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if sub == "log":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return fake_run


def test_push_plugin_requires_existing_plugin_root(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(PushError, match="plugin root does not exist"):
        push_plugin(
            missing,
            repo="owner/name",
            commit_message="franklin: assemble x v0.1.0",
        )


def test_push_plugin_rejects_pr_flag_on_main_branch(tmp_path: Path) -> None:
    plugin_root = _make_plugin_tree(tmp_path)
    with pytest.raises(PushError, match="--pr requires --branch"):
        push_plugin(
            plugin_root,
            repo="owner/name",
            create_pr=True,
            branch="main",
            commit_message="franklin: assemble p v0.1.0",
        )


def test_push_plugin_gh_backend_skips_create_when_repo_exists(tmp_path: Path) -> None:
    plugin_root = _make_plugin_tree(tmp_path)

    commands: list[list[str]] = []
    with (
        patch("franklin.publisher.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "franklin.publisher.subprocess.run",
            side_effect=_make_fake_run(commands, repo_exists=True),
        ),
    ):
        result = push_plugin(
            plugin_root,
            repo="palkan/skills",
            commit_message="franklin: assemble p v0.1.0",
        )

    assert isinstance(result, PushResult)
    assert result.backend == "gh"
    assert result.created_repo is False
    assert result.repo_url == "https://github.com/palkan/skills"
    assert result.branch == "main"
    assert result.pr_url is None

    assert commands[0][:3] == ["gh", "repo", "view"]
    assert not any(cmd[:3] == ["gh", "repo", "create"] for cmd in commands)

    git_subcommands = [_git_subcommand(cmd) for cmd in commands if cmd[0] == "git"]
    assert "init" in git_subcommands
    assert "add" in git_subcommands
    assert "commit" in git_subcommands
    assert "push" in git_subcommands


def test_push_plugin_gh_backend_creates_missing_repo(tmp_path: Path) -> None:
    plugin_root = _make_plugin_tree(tmp_path)

    commands: list[list[str]] = []
    with (
        patch("franklin.publisher.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "franklin.publisher.subprocess.run",
            side_effect=_make_fake_run(commands, repo_exists=False),
        ),
    ):
        result = push_plugin(
            plugin_root,
            repo="palkan/skills",
            public=True,
            commit_message="franklin: assemble p v0.1.0",
        )

    assert result.created_repo is True
    create_cmds = [cmd for cmd in commands if cmd[:3] == ["gh", "repo", "create"]]
    assert len(create_cmds) == 1
    assert "--public" in create_cmds[0]
    assert "--private" not in create_cmds[0]


def test_push_plugin_gh_backend_defaults_to_private(tmp_path: Path) -> None:
    plugin_root = _make_plugin_tree(tmp_path)

    commands: list[list[str]] = []
    with (
        patch("franklin.publisher.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "franklin.publisher.subprocess.run",
            side_effect=_make_fake_run(commands, repo_exists=False),
        ),
    ):
        push_plugin(
            plugin_root,
            repo="palkan/skills",
            commit_message="franklin: assemble p v0.1.0",
        )

    create_cmds = [cmd for cmd in commands if cmd[:3] == ["gh", "repo", "create"]]
    assert "--private" in create_cmds[0]
    assert "--public" not in create_cmds[0]


def test_push_plugin_gh_backend_opens_pr_on_non_main_branch(
    tmp_path: Path,
) -> None:
    plugin_root = _make_plugin_tree(tmp_path)

    commands: list[list[str]] = []
    with (
        patch("franklin.publisher.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "franklin.publisher.subprocess.run",
            side_effect=_make_fake_run(commands),
        ),
    ):
        result = push_plugin(
            plugin_root,
            repo="palkan/skills",
            branch="franklin/update",
            create_pr=True,
            commit_message="franklin: assemble p v0.1.0",
        )

    assert result.pr_url == "https://github.com/palkan/skills/pull/7"
    pr_cmds = [cmd for cmd in commands if cmd[:3] == ["gh", "pr", "create"]]
    assert len(pr_cmds) == 1
    assert "--head" in pr_cmds[0]
    assert "franklin/update" in pr_cmds[0]
    assert "--base" in pr_cmds[0]
    assert "main" in pr_cmds[0]


def test_push_plugin_wraps_tree_in_single_plugin_marketplace(tmp_path: Path) -> None:
    """Published tree must be a marketplace: plugin.json at <name>/, marketplace.json at root."""
    plugin_root = _make_plugin_tree(tmp_path, name="my-plugin")

    commands: list[list[str]] = []
    with (
        patch("franklin.publisher.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "franklin.publisher.subprocess.run",
            side_effect=_make_fake_run(commands, repo_exists=True),
        ),
    ):
        push_plugin(
            plugin_root,
            repo="owner/my-plugin",
            commit_message="franklin: assemble my-plugin v0.1.0",
        )

    workspace = plugin_root.parent / "_publish_my-plugin"
    assert workspace.is_dir()

    marketplace_path = workspace / ".claude-plugin" / "marketplace.json"
    assert marketplace_path.exists()
    manifest = json.loads(marketplace_path.read_text())
    assert manifest["name"] == "my-plugin"
    assert len(manifest["plugins"]) == 1
    assert manifest["plugins"][0]["name"] == "my-plugin"
    assert manifest["plugins"][0]["source"] == "./my-plugin"

    assert (workspace / "my-plugin" / ".claude-plugin" / "plugin.json").exists()
    assert (workspace / "my-plugin" / "SKILL.md").exists()
    assert (workspace / "README.md").exists()


def test_push_plugin_requires_plugin_manifest(tmp_path: Path) -> None:
    plugin_root = tmp_path / "bare"
    plugin_root.mkdir()
    (plugin_root / "README.md").write_text("nope\n")
    with (
        patch("franklin.publisher.shutil.which", return_value="/usr/local/bin/gh"),
        pytest.raises(PushError, match=r"no plugin\.json"),
    ):
        push_plugin(
            plugin_root,
            repo="owner/name",
            commit_message="franklin: assemble x v0.1.0",
        )


def test_push_plugin_surfaces_gh_failure_as_push_error(tmp_path: Path) -> None:
    plugin_root = _make_plugin_tree(tmp_path)

    commands: list[list[str]] = []
    with (
        patch("franklin.publisher.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "franklin.publisher.subprocess.run",
            side_effect=_make_fake_run(
                commands,
                repo_exists=False,
                create_repo_exit=1,
                create_repo_stderr="name already taken",
            ),
        ),
        pytest.raises(PushError, match="gh repo create failed"),
    ):
        push_plugin(
            plugin_root,
            repo="palkan/skills",
            commit_message="franklin: assemble p v0.1.0",
        )
