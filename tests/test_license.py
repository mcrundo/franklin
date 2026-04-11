"""Tests for franklin.license.

The bundled public key (`src/franklin/_license_public_key.pem`) matches
the private key under `tests/fixtures/license_private_key.pem`, so test
tokens can be minted with the fixture key and verified by the real
module path without patching the verifier. Production rotation swaps
the public key and this fixture at the same time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest

from franklin import license as license_mod
from franklin.license import (
    _BYPASS_ENV_VAR,
    _BYPASS_SECRET,
    LicenseError,
    ensure_license,
    login,
    logout,
    whoami,
)

_FIXTURE_PRIVATE_KEY = (
    Path(__file__).parent / "fixtures" / "license_private_key.pem"
).read_bytes()


def _mint(
    *,
    sub: str = "user@example.com",
    features: list[str] | None = None,
    plan: str = "pro",
    exp_delta: timedelta = timedelta(days=365),
    iat_delta: timedelta = timedelta(0),
    jti: str | None = "token-1",
    extra: dict[str, Any] | None = None,
) -> str:
    iat = datetime.now(tz=UTC) + iat_delta
    exp = iat + exp_delta
    claims: dict[str, Any] = {
        "sub": sub,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "features": features if features is not None else ["push", "install"],
        "plan": plan,
    }
    if jti is not None:
        claims["jti"] = jti
    if extra:
        claims.update(extra)
    return jwt.encode(claims, _FIXTURE_PRIVATE_KEY, algorithm="RS256")


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect all on-disk license state into tmp_path for each test."""
    monkeypatch.setenv("FRANKLIN_LICENSE_DIR", str(tmp_path / "franklin-cfg"))
    monkeypatch.delenv(_BYPASS_ENV_VAR, raising=False)

    # Prevent any accidental network calls from hitting the real endpoint.
    monkeypatch.setattr(
        license_mod,
        "_refresh_revocations_opportunistic",
        lambda state: False,
    )


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_verifies_and_persists_token() -> None:
    token = _mint()
    result = login(token)
    assert result.subject == "user@example.com"
    assert result.plan == "pro"
    assert "push" in result.features
    assert "install" in result.features
    assert result.jti == "token-1"

    # File written at the expected path
    path = Path(license_mod._config_dir()) / "license.jwt"
    assert path.exists()
    assert path.read_text().strip() == token


def test_login_rejects_empty_token() -> None:
    with pytest.raises(LicenseError, match="no license token"):
        login("")


def test_login_rejects_expired_token() -> None:
    token = _mint(exp_delta=timedelta(seconds=-1))
    with pytest.raises(LicenseError, match="expired"):
        login(token)


def test_login_rejects_bad_signature() -> None:
    token = _mint()
    tampered = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
    with pytest.raises(LicenseError):
        login(tampered)


def test_login_rejects_non_jwt_garbage() -> None:
    with pytest.raises(LicenseError):
        login("this is not a jwt")


# ---------------------------------------------------------------------------
# logout / whoami
# ---------------------------------------------------------------------------


def test_whoami_returns_none_when_not_logged_in() -> None:
    assert whoami() is None


def test_whoami_returns_license_after_login() -> None:
    login(_mint(sub="alice@example.com"))
    result = whoami()
    assert result is not None
    assert result.subject == "alice@example.com"


def test_logout_removes_license_file() -> None:
    login(_mint())
    assert logout() is True
    assert whoami() is None
    assert logout() is False  # second call is a no-op


# ---------------------------------------------------------------------------
# ensure_license happy path and feature gating
# ---------------------------------------------------------------------------


def test_ensure_license_accepts_valid_license_with_requested_feature() -> None:
    login(_mint(features=["push"]))
    # Mark online now so the grace window is wide open.
    _set_last_online_now()

    result = ensure_license(feature="push")
    assert result is not None
    assert result.subject == "user@example.com"


def test_ensure_license_raises_when_no_license_installed() -> None:
    with pytest.raises(LicenseError, match="no franklin license installed"):
        ensure_license(feature="push")


def test_ensure_license_raises_when_feature_missing() -> None:
    login(_mint(features=["install"]))
    _set_last_online_now()
    with pytest.raises(LicenseError, match="does not grant"):
        ensure_license(feature="push")


def test_ensure_license_raises_when_token_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(_mint(jti="revoked-one"))

    # Simulate a refresh that marks the jti as revoked.
    def fake_refresh(state: license_mod._LocalState) -> bool:
        state.revoked_jtis = ["revoked-one"]
        state.last_online_at = datetime.now(tz=UTC)
        license_mod._save_state(state)
        return True

    monkeypatch.setattr(license_mod, "_refresh_revocations_opportunistic", fake_refresh)

    with pytest.raises(LicenseError, match="revoked"):
        ensure_license(feature="push")


# ---------------------------------------------------------------------------
# Grace window semantics
# ---------------------------------------------------------------------------


def _set_last_online(delta_days: int) -> None:
    """Write a state.json with last_online_at set delta_days ago."""
    state = license_mod._load_state()
    state.last_online_at = datetime.now(tz=UTC) - timedelta(days=delta_days)
    license_mod._save_state(state)


def _set_last_online_now() -> None:
    _set_last_online(0)


def test_ensure_license_soft_grace_is_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    login(_mint())
    _set_last_online(10)

    ensure_license(feature="push")
    captured = capsys.readouterr()
    assert "days" not in captured.err


def test_ensure_license_hard_grace_warns_but_allows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    login(_mint())
    _set_last_online(30)

    result = ensure_license(feature="push")
    assert result is not None
    captured = capsys.readouterr()
    assert "30 days" in captured.err


def test_ensure_license_beyond_hard_grace_blocks() -> None:
    login(_mint())
    _set_last_online(75)

    with pytest.raises(LicenseError, match="hard grace"):
        ensure_license(feature="push")


def test_ensure_license_without_any_online_check_blocks() -> None:
    login(_mint())
    # login() sets last_online_at to now, so drop it to simulate never-online.
    state = license_mod._load_state()
    state.last_online_at = None
    license_mod._save_state(state)

    with pytest.raises(LicenseError, match="no successful online check"):
        ensure_license(feature="push")


# ---------------------------------------------------------------------------
# Bypass escape hatch
# ---------------------------------------------------------------------------


def test_bypass_allows_command_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(_BYPASS_ENV_VAR, _BYPASS_SECRET)
    result = ensure_license(feature="push")
    assert result is None  # bypass returns None, not a license
    captured = capsys.readouterr()
    assert "bypass" in captured.err


def test_bypass_with_wrong_secret_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_BYPASS_ENV_VAR, "wrong-value")
    with pytest.raises(LicenseError, match="no franklin license installed"):
        ensure_license(feature="push")


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


def test_login_writes_license_file_with_owner_only_mode() -> None:
    login(_mint())
    path = Path(license_mod._config_dir()) / "license.jwt"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
