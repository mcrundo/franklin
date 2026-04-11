"""Tests for ``franklin.license.status`` and the ``franklin license status`` command (RUB-83)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from typer.testing import CliRunner

from franklin import license as license_mod
from franklin.cli import app
from franklin.license import (
    _BYPASS_ENV_VAR,
    _BYPASS_SECRET,
    LicenseHealth,
    login,
    status,
)

_FIXTURE_PRIVATE_KEY = (
    Path(__file__).parent / "fixtures" / "license_private_key.pem"
).read_bytes()


def _mint(
    *,
    exp_delta: timedelta = timedelta(days=365),
    jti: str | None = "token-1",
    features: list[str] | None = None,
) -> str:
    now = datetime.now(tz=UTC)
    claims: dict[str, Any] = {
        "sub": "user@example.com",
        "iat": int(now.timestamp()),
        "exp": int((now + exp_delta).timestamp()),
        "features": features if features is not None else ["push", "install"],
        "plan": "pro",
    }
    if jti is not None:
        claims["jti"] = jti
    return jwt.encode(claims, _FIXTURE_PRIVATE_KEY, algorithm="RS256")


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRANKLIN_LICENSE_DIR", str(tmp_path / "franklin-cfg"))
    monkeypatch.delenv(_BYPASS_ENV_VAR, raising=False)
    # Prevent network calls
    monkeypatch.setattr(
        license_mod,
        "_refresh_revocations_opportunistic",
        lambda state: False,
    )


def _set_last_online(delta_days: int) -> None:
    state = license_mod._load_state()
    state.last_online_at = datetime.now(tz=UTC) - timedelta(days=delta_days)
    license_mod._save_state(state)


runner = CliRunner()


# ---------------------------------------------------------------------------
# status() pure function
# ---------------------------------------------------------------------------


def test_status_no_license() -> None:
    result = status()
    assert result.health == LicenseHealth.NO_LICENSE
    assert result.license is None
    assert "login" in result.next_step


def test_status_valid_and_fresh() -> None:
    login(_mint())
    _set_last_online(0)

    result = status()
    assert result.health == LicenseHealth.VALID
    assert result.license is not None
    assert result.grace_band == "fresh"
    assert result.days_since_online == 0
    assert result.bypass_active is False
    assert result.days_until_expiry is not None
    assert result.days_until_expiry > 350


def test_status_in_hard_grace() -> None:
    login(_mint())
    _set_last_online(30)

    result = status()
    assert result.health == LicenseHealth.HARD_GRACE
    assert result.grace_band == "hard"
    assert "30 days" in result.next_step


def test_status_past_hard_grace_blocks() -> None:
    login(_mint())
    _set_last_online(75)

    result = status()
    assert result.health == LicenseHealth.BLOCKED_HARD_GRACE
    assert result.grace_band == "exceeded"
    assert "refresh" in result.next_step


def test_status_expired_license() -> None:
    login(_mint(exp_delta=timedelta(days=1)))
    # Manually rewrite the license file to a past-expiry token
    expired_token = _mint(exp_delta=timedelta(seconds=-1))
    (Path(license_mod._config_dir()) / "license.jwt").write_text(expired_token)

    result = status()
    assert result.health == LicenseHealth.BLOCKED_EXPIRED
    assert result.license is None
    assert result.detail is not None and "expired" in result.detail.lower()


def test_status_corrupt_license_file() -> None:
    cfg = Path(license_mod._config_dir())
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "license.jwt").write_text("this is not a jwt at all")

    result = status()
    assert result.health == LicenseHealth.CORRUPT_LICENSE
    assert result.detail is not None


def test_status_revoked_jti() -> None:
    login(_mint(jti="revoked-one"))
    _set_last_online(0)
    state = license_mod._load_state()
    state.revoked_jtis = ["revoked-one"]
    license_mod._save_state(state)

    result = status()
    assert result.health == LicenseHealth.BLOCKED_REVOKED
    assert "support" in result.next_step


def test_status_no_online_check_blocks() -> None:
    login(_mint())
    state = license_mod._load_state()
    state.last_online_at = None
    license_mod._save_state(state)

    result = status()
    assert result.health == LicenseHealth.BLOCKED_NO_ONLINE_CHECK
    assert result.days_since_online is None
    assert result.grace_band == "unknown"


def test_status_bypass_active_reports_underlying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(_mint())
    _set_last_online(30)
    monkeypatch.setenv(_BYPASS_ENV_VAR, _BYPASS_SECRET)

    result = status()
    assert result.health == LicenseHealth.BYPASS_ACTIVE
    assert result.bypass_active is True
    assert result.underlying_health == LicenseHealth.HARD_GRACE


def test_status_bypass_active_without_license(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_BYPASS_ENV_VAR, _BYPASS_SECRET)

    result = status()
    assert result.health == LicenseHealth.BYPASS_ACTIVE
    assert result.underlying_health == LicenseHealth.NO_LICENSE


# ---------------------------------------------------------------------------
# CLI wiring — franklin license status
# ---------------------------------------------------------------------------


def test_cli_status_no_license() -> None:
    result = runner.invoke(app, ["license", "status"])
    assert result.exit_code == 0
    assert "no license installed" in result.output


def test_cli_status_valid_license() -> None:
    login(_mint())
    _set_last_online(0)
    result = runner.invoke(app, ["license", "status"])
    assert result.exit_code == 0
    assert "valid" in result.output
    assert "user@example.com" in result.output


def test_cli_status_json_output() -> None:
    login(_mint())
    _set_last_online(0)
    result = runner.invoke(app, ["license", "status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["health"] == "valid"
    assert data["license"]["subject"] == "user@example.com"
    assert data["grace_band"] == "fresh"


def test_cli_status_refresh_success(monkeypatch: pytest.MonkeyPatch) -> None:
    login(_mint())
    _set_last_online(20)

    def fake_refresh(state: license_mod._LocalState) -> bool:
        state.last_online_at = datetime.now(tz=UTC)
        state.revoked_jtis = []
        license_mod._save_state(state)
        return True

    monkeypatch.setattr(license_mod, "_refresh_revocations_opportunistic", fake_refresh)

    result = runner.invoke(app, ["license", "status", "--refresh"])
    assert result.exit_code == 0
    assert "refresh succeeded" in result.output
    assert "valid" in result.output


def test_cli_status_refresh_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    login(_mint())
    _set_last_online(5)
    monkeypatch.setattr(
        license_mod,
        "_refresh_revocations_opportunistic",
        lambda state: False,
    )

    result = runner.invoke(app, ["license", "status", "--refresh"])
    assert result.exit_code == 0
    assert "refresh failed" in result.output
    # Still renders the cached state (valid) below the failure note
    assert "valid" in result.output


def test_cli_status_never_exits_nonzero_on_corrupt_file() -> None:
    cfg = Path(license_mod._config_dir())
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "license.jwt").write_text("corrupt-token")

    result = runner.invoke(app, ["license", "status"])
    assert result.exit_code == 0
    assert "corrupt" in result.output
