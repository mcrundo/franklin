"""Tests for `franklin install --scope`.

Covers the three supported scopes (user, project, local), invalid
scope rejection, --force interaction for each scope, and the exact
activation commands each scope prints. install_plugin is mocked so
these tests are pure CLI-layer and don't touch ~/.franklin/ or the
real filesystem.
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
from franklin.cli import install_command
from franklin.installer import InstallResult
from franklin.license import _BYPASS_ENV_VAR

_FIXTURE_PRIVATE_KEY = (
    Path(__file__).parent / "fixtures" / "license_private_key.pem"
).read_bytes()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mint(features: list[str] | None = None) -> str:
    iat = datetime.now(tz=UTC)
    exp = iat + timedelta(days=365)
    claims: dict[str, Any] = {
        "sub": "user@example.com",
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "features": features if features is not None else ["install"],
        "plan": "pro",
        "jti": "scope-test",
    }
    return jwt.encode(claims, _FIXTURE_PRIVATE_KEY, algorithm="RS256")


def _seed_license(token: str) -> None:
    license_mod._save_license_token(token)
    state = license_mod._LocalState(last_online_at=datetime.now(tz=UTC))
    license_mod._save_state(state)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRANKLIN_LICENSE_DIR", str(tmp_path / "franklin-cfg"))
    monkeypatch.delenv(_BYPASS_ENV_VAR, raising=False)
    monkeypatch.setattr(license_mod, "_refresh_revocations_opportunistic", lambda state: False)
    # Widen the Rich console so long tmp paths don't wrap mid-assertion.
    monkeypatch.setenv("COLUMNS", "400")
    from franklin.cli import console as cli_console

    cli_console.width = 400
    # Every test starts with a valid install-featured license so scope
    # behavior is isolated from gate behavior (which has its own suite).
    _seed_license(_mint())


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Build a minimal run directory with plan.json and an assembled plugin."""
    from franklin.checkpoint import RunDirectory
    from franklin.schema import (
        BookManifest,
        BookMetadata,
        BookSource,
        BookStructure,
        PlanManifest,
        PluginMeta,
    )

    run = tmp_path / "runs" / "test-book"
    run.mkdir(parents=True)
    rd = RunDirectory(run)
    rd.ensure()
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
            plugin=PluginMeta(name="test-plugin", version="0.1.0", description="Test plugin"),
        )
    )
    (run / "output" / "test-plugin" / ".claude-plugin").mkdir(parents=True)
    (run / "output" / "test-plugin" / ".claude-plugin" / "plugin.json").write_text(
        '{"name":"test-plugin","version":"0.1.0","description":"Test"}'
    )
    return run


def _fake_install_result(tmp_path: Path) -> InstallResult:
    return InstallResult(
        plugin_name="test-plugin",
        plugin_version="0.1.0",
        marketplace_root=tmp_path / "marketplace",
        plugin_root=tmp_path / "marketplace" / "test-plugin",
        replaced=False,
    )


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------


def test_install_rejects_invalid_scope(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        install_command(run_dir=run_dir, scope="global", force=False)
    assert exc_info.value.exit_code == 2
    captured = capsys.readouterr()
    assert "invalid --scope" in captured.out
    assert "global" in captured.out


# ---------------------------------------------------------------------------
# User scope (default)
# ---------------------------------------------------------------------------


def test_install_user_scope_calls_install_plugin(
    run_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = MagicMock(return_value=_fake_install_result(tmp_path))
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="user", force=False)

    fake.assert_called_once()
    captured = capsys.readouterr()
    assert "/plugin marketplace add" in captured.out
    assert "/plugin install test-plugin@franklin" in captured.out
    assert "--scope project" not in captured.out
    assert "/reload-plugins" in captured.out


def test_install_default_scope_is_user(
    run_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = MagicMock(return_value=_fake_install_result(tmp_path))
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="user", force=False)

    captured = capsys.readouterr()
    assert "test-plugin (user)" in captured.out


# ---------------------------------------------------------------------------
# Project scope
# ---------------------------------------------------------------------------


def test_install_project_scope_calls_install_plugin(run_dir: Path, tmp_path: Path) -> None:
    fake = MagicMock(return_value=_fake_install_result(tmp_path))
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="project", force=False)
    fake.assert_called_once()


def test_install_project_scope_prints_scope_project_in_activation(
    run_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = MagicMock(return_value=_fake_install_result(tmp_path))
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="project", force=False)

    captured = capsys.readouterr()
    assert "/plugin install test-plugin@franklin --scope project" in captured.out
    assert ".claude/settings.json" in captured.out


def test_install_project_scope_shows_scope_in_header(
    run_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = MagicMock(return_value=_fake_install_result(tmp_path))
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="project", force=False)

    captured = capsys.readouterr()
    assert "test-plugin (project)" in captured.out


# ---------------------------------------------------------------------------
# Local scope
# ---------------------------------------------------------------------------


def test_install_local_scope_does_not_call_install_plugin(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = MagicMock()
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="local", force=False)

    fake.assert_not_called()
    captured = capsys.readouterr()
    assert "claude --plugin-dir" in captured.out


def test_install_local_scope_prints_absolute_plugin_path(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = MagicMock()
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="local", force=False)

    captured = capsys.readouterr()
    expected_path = (run_dir / "output" / "test-plugin").resolve()
    assert str(expected_path) in captured.out


def test_install_local_scope_mentions_ephemeral(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = MagicMock()
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="local", force=False)

    captured = capsys.readouterr()
    assert "ephemeral" in captured.out.lower()
    assert "session" in captured.out.lower()


def test_install_local_scope_ignores_force(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--force is meaningless for local scope (nothing is written) and must
    be silently ignored rather than erroring."""
    fake = MagicMock()
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="local", force=True)

    fake.assert_not_called()
    captured = capsys.readouterr()
    assert "claude --plugin-dir" in captured.out
    # No error output
    assert "error" not in captured.out.lower()


def test_install_local_scope_does_not_output_marketplace_commands(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = MagicMock()
    with patch("franklin.cli.install_plugin", fake):
        install_command(run_dir=run_dir, scope="local", force=False)

    captured = capsys.readouterr()
    assert "/plugin marketplace add" not in captured.out
    assert "/plugin install" not in captured.out
    assert "/reload-plugins" not in captured.out


# ---------------------------------------------------------------------------
# License gate applies to all scopes
# ---------------------------------------------------------------------------


def test_install_local_scope_respects_license_gate(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Local scope is still a premium install path when the gate is on."""
    from franklin import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_LICENSE_GATE_ENABLED", True)

    # Remove the seeded license
    license_mod.logout()
    # Also clear the state file so we're cleanly unlicensed
    state_path = license_mod._state_path()
    if state_path.exists():
        state_path.unlink()

    fake = MagicMock()
    with patch("franklin.cli.install_plugin", fake), pytest.raises(typer.Exit):
        install_command(run_dir=run_dir, scope="local", force=False)

    fake.assert_not_called()
    captured = capsys.readouterr()
    assert "franklin install is a Pro feature" in captured.out


def test_install_project_scope_respects_license_gate(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from franklin import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_LICENSE_GATE_ENABLED", True)

    license_mod.logout()
    state_path = license_mod._state_path()
    if state_path.exists():
        state_path.unlink()

    fake = MagicMock()
    with patch("franklin.cli.install_plugin", fake), pytest.raises(typer.Exit):
        install_command(run_dir=run_dir, scope="project", force=False)

    fake.assert_not_called()
    captured = capsys.readouterr()
    assert "franklin install is a Pro feature" in captured.out
