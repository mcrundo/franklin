"""Tests for franklin doctor preflight."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from franklin import doctor
from franklin.cli import app
from franklin.doctor import (
    CheckResult,
    CheckStatus,
    has_failures,
    run_checks,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never actually touch the network during unit tests."""
    monkeypatch.setattr(
        doctor,
        "_check_network_to_anthropic",
        lambda: CheckResult(
            "Network → api.anthropic.com",
            CheckStatus.OK,
            "stubbed",
        ),
    )
    # Re-bind the default-check tuple so the orchestrator uses the stub.
    monkeypatch.setattr(
        doctor,
        "_DEFAULT_CHECKS",
        (
            doctor._check_python_version,
            doctor._check_uv_available,
            doctor._check_api_key,
            doctor._check_license,
            doctor._check_claude_binary,
            doctor._check_network_to_anthropic,
            doctor._check_disk_space,
        ),
    )


@pytest.fixture(autouse=True)
def _isolated_license(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRANKLIN_LICENSE_DIR", str(tmp_path / "franklin-cfg"))
    monkeypatch.delenv("FRANKLIN_LICENSE_BYPASS", raising=False)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def test_python_version_check_passes() -> None:
    result = doctor._check_python_version()
    assert result.status == CheckStatus.OK


def test_api_key_check_ok_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-abcd1234")
    result = doctor._check_api_key()
    assert result.status == CheckStatus.OK
    assert "1234" in result.detail


def test_api_key_check_fails_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        doctor,
        "resolve_anthropic_api_key",
        lambda: (_ for _ in ()).throw(RuntimeError("no key")),
    )
    result = doctor._check_api_key()
    assert result.status == CheckStatus.FAIL


def test_license_check_warns_when_no_license() -> None:
    result = doctor._check_license()
    assert result.status == CheckStatus.WARN
    assert "no license" in result.detail


def test_claude_binary_missing_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor._check_claude_binary()
    assert result.status == CheckStatus.WARN


def test_disk_space_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import namedtuple

    Usage = namedtuple("Usage", ["total", "used", "free"])
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: Usage(100, 50, 10**12))
    result = doctor._check_disk_space()
    assert result.status == CheckStatus.OK


def test_disk_space_warn_when_low(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import namedtuple

    Usage = namedtuple("Usage", ["total", "used", "free"])
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: Usage(100, 50, 10**8))
    result = doctor._check_disk_space()
    assert result.status == CheckStatus.WARN


# ---------------------------------------------------------------------------
# run_checks orchestrator
# ---------------------------------------------------------------------------


def test_run_checks_includes_network_by_default() -> None:
    results = run_checks()
    names = [r.name for r in results]
    assert any("Network" in n for n in names)


def test_run_checks_skips_network_when_requested() -> None:
    results = run_checks(skip_network=True)
    names = [r.name for r in results]
    assert not any("Network" in n for n in names)


def test_has_failures_detects_fail() -> None:
    assert has_failures([CheckResult("x", CheckStatus.FAIL, "boom")])
    assert not has_failures([CheckResult("x", CheckStatus.WARN, "meh")])


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_doctor_renders_all_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-abcd1234")
    result = runner.invoke(app, ["doctor", "--skip-network"])
    # Exit 0 if no failures, 1 if there are. We only assert it didn't crash.
    assert result.exit_code in (0, 1)
    assert "Python version" in result.output
    assert "Anthropic API key" in result.output


def test_cli_doctor_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-abcd1234")
    result = runner.invoke(app, ["doctor", "--skip-network", "--json"])
    assert result.exit_code in (0, 1)
    data = json.loads(result.output)
    assert any(r["name"] == "Python version" for r in data)
    for r in data:
        assert r["status"] in {"ok", "warn", "fail"}
