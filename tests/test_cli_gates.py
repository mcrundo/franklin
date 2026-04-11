"""Tests for the license gate on franklin push and franklin install.

These tests verify that the two premium commands call ensure_license
before doing any work, surface a friendly multi-line error (no stack
trace) when the gate fails, and respect the bypass env var. The
underlying push_plugin and install_plugin functions are mocked out —
they have their own test suites; these tests are strictly about the
gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
import typer

from franklin import license as license_mod
from franklin.cli import install_command, push_command
from franklin.installer import InstallResult
from franklin.license import _BYPASS_ENV_VAR, _BYPASS_SECRET
from franklin.publisher import PushResult
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    PlanManifest,
    PluginMeta,
)

_FIXTURE_PRIVATE_KEY = (
    Path(__file__).parent / "fixtures" / "license_private_key.pem"
).read_bytes()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mint(
    *,
    features: list[str] | None = None,
    exp_delta: timedelta = timedelta(days=365),
) -> str:
    iat = datetime.now(tz=UTC)
    exp = iat + exp_delta
    claims: dict[str, Any] = {
        "sub": "user@example.com",
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "features": features if features is not None else ["push", "install"],
        "plan": "pro",
        "jti": "cli-gate-test",
    }
    return jwt.encode(claims, _FIXTURE_PRIVATE_KEY, algorithm="RS256")


def _seed_license(token: str) -> None:
    """Write the given token into the isolated license directory."""
    license_mod._save_license_token(token)
    state = license_mod._LocalState(last_online_at=datetime.now(tz=UTC))
    license_mod._save_state(state)


@pytest.fixture(autouse=True)
def _isolated_license_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRANKLIN_LICENSE_DIR", str(tmp_path / "franklin-cfg"))
    monkeypatch.delenv(_BYPASS_ENV_VAR, raising=False)
    monkeypatch.setattr(
        license_mod,
        "_refresh_revocations_opportunistic",
        lambda state: False,
    )
    # v0.1 ships with the license gate disabled; these tests exist to
    # prove the gate logic still works, so they re-enable it locally.
    from franklin import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_LICENSE_GATE_ENABLED", True)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Build a minimal run directory with plan.json and an assembled plugin."""
    run = tmp_path / "runs" / "test-book"
    (run / "output" / "test-plugin").mkdir(parents=True)
    (run / "output" / "test-plugin" / ".claude-plugin").mkdir()
    (run / "output" / "test-plugin" / ".claude-plugin" / "plugin.json").write_text(
        '{"name":"test-plugin","version":"0.1.0","description":"x"}'
    )
    (run / "chapters").mkdir()
    (run / "raw").mkdir()

    from franklin.checkpoint import RunDirectory

    rd = RunDirectory(run)
    rd.save_book(
        BookManifest(
            franklin_version="0.1.0",
            source=BookSource(
                path="x.epub",
                sha256="0" * 64,
                format="epub",
                ingested_at=datetime.now(UTC),
            ),
            metadata=BookMetadata(title="Test", authors=["Ada"]),
            structure=BookStructure(),
        )
    )
    rd.save_plan(
        PlanManifest(
            book_id="test",
            generated_at=datetime.now(UTC),
            planner_model="claude-opus-4-6",
            planner_rationale="test",
            plugin=PluginMeta(
                name="test-plugin",
                version="0.1.0",
                description="Test plugin",
            ),
        )
    )
    return run


# ---------------------------------------------------------------------------
# push command gate
# ---------------------------------------------------------------------------


def test_push_command_proceeds_with_valid_license(
    run_dir: Path,
) -> None:
    _seed_license(_mint(features=["push"]))

    fake_push = MagicMock(
        return_value=PushResult(
            repo_url="https://github.com/owner/name",
            branch="main",
            created_repo=False,
            pr_url=None,
            backend="gh",
        )
    )
    with patch("franklin.cli.push_plugin", fake_push):
        push_command(
            run_dir=run_dir,
            repo="owner/name",
            branch="main",
            create_pr=False,
            public=False,
        )

    fake_push.assert_called_once()


def test_push_command_blocks_with_missing_license(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_push = MagicMock()
    with patch("franklin.cli.push_plugin", fake_push), pytest.raises(typer.Exit) as exc_info:
        push_command(
            run_dir=run_dir,
            repo="owner/name",
            branch="main",
            create_pr=False,
            public=False,
        )

    assert exc_info.value.exit_code == 1
    fake_push.assert_not_called()

    captured = capsys.readouterr()
    assert "franklin push is a Pro feature" in captured.out
    assert "no franklin license installed" in captured.out
    assert "franklin license login" in captured.out
    assert "https://" in captured.out  # pricing URL


def test_push_command_blocks_with_expired_license(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_license(_mint(exp_delta=timedelta(seconds=-1)))

    fake_push = MagicMock()
    with patch("franklin.cli.push_plugin", fake_push), pytest.raises(typer.Exit) as exc_info:
        push_command(
            run_dir=run_dir,
            repo="owner/name",
            branch="main",
            create_pr=False,
            public=False,
        )

    assert exc_info.value.exit_code == 1
    fake_push.assert_not_called()

    captured = capsys.readouterr()
    assert "Pro feature" in captured.out
    assert "expired" in captured.out


def test_push_command_blocks_when_feature_not_granted(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_license(_mint(features=["install"]))  # install only, not push

    fake_push = MagicMock()
    with patch("franklin.cli.push_plugin", fake_push), pytest.raises(typer.Exit):
        push_command(
            run_dir=run_dir,
            repo="owner/name",
            branch="main",
            create_pr=False,
            public=False,
        )

    fake_push.assert_not_called()
    captured = capsys.readouterr()
    assert "does not grant" in captured.out


def test_push_command_proceeds_when_bypass_active(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_BYPASS_ENV_VAR, _BYPASS_SECRET)

    fake_push = MagicMock(
        return_value=PushResult(
            repo_url="https://github.com/owner/name",
            branch="main",
            created_repo=False,
            pr_url=None,
            backend="gh",
        )
    )
    with patch("franklin.cli.push_plugin", fake_push):
        push_command(
            run_dir=run_dir,
            repo="owner/name",
            branch="main",
            create_pr=False,
            public=False,
        )

    fake_push.assert_called_once()


# ---------------------------------------------------------------------------
# install command gate
# ---------------------------------------------------------------------------


def test_install_command_proceeds_with_valid_license(
    run_dir: Path,
    tmp_path: Path,
) -> None:
    _seed_license(_mint(features=["install"]))

    fake_install = MagicMock(
        return_value=InstallResult(
            plugin_name="test-plugin",
            plugin_version="0.1.0",
            marketplace_root=tmp_path / "mp",
            plugin_root=tmp_path / "mp" / "test-plugin",
            replaced=False,
        )
    )
    with patch("franklin.cli.install_plugin", fake_install):
        install_command(run_dir=run_dir, scope="user", force=False)

    fake_install.assert_called_once()


def test_install_command_blocks_with_missing_license(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_install = MagicMock()
    with (
        patch("franklin.cli.install_plugin", fake_install),
        pytest.raises(typer.Exit) as exc_info,
    ):
        install_command(run_dir=run_dir, scope="user", force=False)

    assert exc_info.value.exit_code == 1
    fake_install.assert_not_called()

    captured = capsys.readouterr()
    assert "franklin install is a Pro feature" in captured.out
    assert "no franklin license installed" in captured.out


def test_install_command_blocks_when_feature_not_granted(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_license(_mint(features=["push"]))  # push only, not install

    fake_install = MagicMock()
    with patch("franklin.cli.install_plugin", fake_install), pytest.raises(typer.Exit):
        install_command(run_dir=run_dir, scope="user", force=False)

    fake_install.assert_not_called()
    captured = capsys.readouterr()
    assert "does not grant" in captured.out


def test_install_command_proceeds_when_bypass_active(
    run_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_BYPASS_ENV_VAR, _BYPASS_SECRET)

    fake_install = MagicMock(
        return_value=InstallResult(
            plugin_name="test-plugin",
            plugin_version="0.1.0",
            marketplace_root=tmp_path / "mp",
            plugin_root=tmp_path / "mp" / "test-plugin",
            replaced=False,
        )
    )
    with patch("franklin.cli.install_plugin", fake_install):
        install_command(run_dir=run_dir, scope="user", force=False)

    fake_install.assert_called_once()


# ---------------------------------------------------------------------------
# No stack trace reaches the user on gate failure
# ---------------------------------------------------------------------------


def test_gate_failure_output_contains_no_traceback_markers(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The friendly error must not leak any traceback-like text."""
    with pytest.raises(typer.Exit):
        push_command(
            run_dir=run_dir,
            repo="owner/name",
            branch="main",
            create_pr=False,
            public=False,
        )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" not in combined
    assert "LicenseError" not in combined
    assert "line " not in combined  # e.g. "line 42, in ..."
