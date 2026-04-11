"""License verification for franklin's premium commands.

Each franklin license is an RS256-signed JWT. The signing key lives in
franklin's issuance infrastructure; the matching public key is bundled
with the package as `_license_public_key.pem` and is the only thing
`ensure_license` trusts to verify a token.

Flow:

1. User runs `franklin license login` and pastes a JWT. We verify it
   against the bundled public key, fail hard if the signature or `exp`
   claim doesn't hold up, and write the raw token to
   `~/.config/franklin/license.jwt` at mode 0600.
2. `ensure_license(feature=...)` is called by premium commands before
   they run. It re-verifies the stored token, checks `exp`, consults a
   cached revocation list for the token's `jti`, then checks the
   offline-grace window.
3. Once per invocation (at most), `ensure_license` opportunistically
   phones home to refresh the revocation list. Network failures are
   swallowed — the phone-home never blocks a command — and the cached
   list is used instead.

Offline grace:

- Up to **14 days** since the last successful online check: license
  remains fully valid, no warning shown.
- **15 to 60 days**: hard grace — the license is still accepted but
  `ensure_license` prints a warning that the user hasn't been online
  recently.
- **Over 60 days**: `ensure_license` refuses and tells the user to
  run `franklin license login` again.

Bypass escape hatch:

Set `FRANKLIN_LICENSE_BYPASS` to the value of `_BYPASS_SECRET` below to
skip all license checks for the current process. Intended only for
support emergencies. When active, `ensure_license` prints a dim warning
but never blocks. The secret is rotated by changing the constant and
shipping a new release. The current value is documented in internal
`SUPPORT.md` (not shipped with the package).

RUB-78 wires `ensure_license` into the push and install commands. This
module builds the mechanism; the gate itself lives in the command
callsites.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib import error, request

import jwt

# ---------------------------------------------------------------------------
# Module-level configuration (tunable via env vars for tests)
# ---------------------------------------------------------------------------

_CONFIG_DIR_ENV = "FRANKLIN_LICENSE_DIR"
_BYPASS_ENV_VAR = "FRANKLIN_LICENSE_BYPASS"

# Rotate this value and ship a new release to invalidate older support
# bypasses. Documented in SUPPORT.md (repo root, not shipped with the
# package). Do not log, print, or persist this constant anywhere.
_BYPASS_SECRET = "ROTATE-ME-ON-RELEASE"

_GRACE_SOFT_DAYS = 14
_GRACE_HARD_DAYS = 60

_REVOCATION_ENDPOINT = "https://franklin.example.com/licenses/revocations.json"
_REVOCATION_FETCH_TIMEOUT_SECONDS = 3.0

_PUBLIC_KEY_PATH = Path(__file__).parent / "_license_public_key.pem"
_PUBLIC_KEY = _PUBLIC_KEY_PATH.read_bytes()


# ---------------------------------------------------------------------------
# Errors and data classes
# ---------------------------------------------------------------------------


class LicenseError(RuntimeError):
    """Raised when a license is missing, invalid, expired, or revoked."""


@dataclass(frozen=True)
class License:
    """A verified franklin license loaded from disk."""

    token: str
    subject: str
    features: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    jti: str | None
    plan: str | None
    raw_claims: dict[str, Any]


class LicenseHealth(StrEnum):
    """Operational health states reported by ``status``.

    These map one-to-one to the next-step messaging in the CLI. They are
    exhaustive — ``status`` never returns anything outside this set.
    """

    VALID = "valid"
    HARD_GRACE = "hard grace"
    BLOCKED_EXPIRED = "blocked (expired)"
    BLOCKED_REVOKED = "blocked (revoked)"
    BLOCKED_HARD_GRACE = "blocked (hard grace exceeded)"
    BLOCKED_NO_ONLINE_CHECK = "blocked (no online check)"
    NO_LICENSE = "no license installed"
    CORRUPT_LICENSE = "corrupt license file"
    BYPASS_ACTIVE = "bypass active"


@dataclass(frozen=True)
class LicenseStatus:
    """Everything ``franklin license status`` reports, pure data.

    ``license`` is the loaded license when one is installed and parses,
    ``None`` otherwise. ``underlying_health`` only appears in bypass mode
    so the caller can still show what would happen without bypass.
    """

    health: LicenseHealth
    license: License | None
    days_until_expiry: int | None
    days_since_online: int | None
    grace_band: str  # "fresh", "soft", "hard", "exceeded", "unknown"
    bypass_active: bool
    next_step: str
    detail: str | None = None
    underlying_health: LicenseHealth | None = None

    def to_dict(self) -> dict[str, Any]:
        license_payload: dict[str, Any] | None = None
        if self.license is not None:
            license_payload = {
                "subject": self.license.subject,
                "plan": self.license.plan,
                "features": list(self.license.features),
                "issued_at": self.license.issued_at.isoformat(),
                "expires_at": self.license.expires_at.isoformat(),
                "jti": self.license.jti,
            }
        return {
            "health": self.health.value,
            "license": license_payload,
            "days_until_expiry": self.days_until_expiry,
            "days_since_online": self.days_since_online,
            "grace_band": self.grace_band,
            "bypass_active": self.bypass_active,
            "next_step": self.next_step,
            "detail": self.detail,
            "underlying_health": (
                self.underlying_health.value if self.underlying_health else None
            ),
        }


@dataclass
class _LocalState:
    """Local state tracked between ensure_license invocations."""

    last_online_at: datetime | None = None
    revoked_jtis: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    override = os.environ.get(_CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "franklin"
    return Path.home() / ".config" / "franklin"


def _license_path() -> Path:
    return _config_dir() / "license.jwt"


def _state_path() -> Path:
    return _config_dir() / "state.json"


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def _verify_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT against the bundled public key."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            _PUBLIC_KEY,
            algorithms=["RS256"],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise LicenseError("license has expired — run `franklin license login`") from exc
    except jwt.InvalidSignatureError as exc:
        raise LicenseError("license signature is invalid") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise LicenseError(f"license is missing a required claim: {exc.claim}") from exc
    except jwt.InvalidTokenError as exc:
        raise LicenseError(f"license is not a valid JWT: {exc}") from exc
    return claims


def _license_from_claims(token: str, claims: dict[str, Any]) -> License:
    features_raw = claims.get("features", [])
    features: tuple[str, ...] = (
        tuple(str(f) for f in features_raw) if isinstance(features_raw, list) else ()
    )
    return License(
        token=token,
        subject=str(claims["sub"]),
        features=features,
        issued_at=datetime.fromtimestamp(int(claims["iat"]), tz=UTC),
        expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=UTC),
        jti=str(claims["jti"]) if "jti" in claims else None,
        plan=str(claims["plan"]) if "plan" in claims else None,
        raw_claims=dict(claims),
    )


# ---------------------------------------------------------------------------
# License file IO
# ---------------------------------------------------------------------------


def _save_license_token(token: str) -> Path:
    path = _license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def _load_license() -> License | None:
    path = _license_path()
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        return None
    claims = _verify_token(token)
    return _license_from_claims(token, claims)


# ---------------------------------------------------------------------------
# Local state (online-check timestamp + cached revocation list)
# ---------------------------------------------------------------------------


def _load_state() -> _LocalState:
    path = _state_path()
    if not path.exists():
        return _LocalState()
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _LocalState()
    if not isinstance(raw, dict):
        return _LocalState()

    last_online: datetime | None = None
    iso = raw.get("last_online_at")
    if isinstance(iso, str):
        try:
            last_online = datetime.fromisoformat(iso)
        except ValueError:
            last_online = None

    revoked = raw.get("revoked_jtis", [])
    if not isinstance(revoked, list):
        revoked = []
    revoked_clean = [str(j) for j in revoked if isinstance(j, str)]

    return _LocalState(last_online_at=last_online, revoked_jtis=revoked_clean)


def _save_state(state: _LocalState) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_online_at": state.last_online_at.isoformat() if state.last_online_at else None,
        "revoked_jtis": list(state.revoked_jtis),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _refresh_revocations_opportunistic(state: _LocalState) -> bool:
    """Try to refresh the revocation list. Return True on success."""
    try:
        req = request.Request(
            _REVOCATION_ENDPOINT,
            headers={"Accept": "application/json"},
        )
        with request.urlopen(req, timeout=_REVOCATION_FETCH_TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except (error.URLError, TimeoutError, OSError):
        return False

    try:
        data: Any = json.loads(body)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False

    revoked = data.get("revoked", [])
    if not isinstance(revoked, list):
        return False
    state.revoked_jtis = [str(j) for j in revoked if isinstance(j, str)]
    state.last_online_at = datetime.now(tz=UTC)
    _save_state(state)
    return True


# ---------------------------------------------------------------------------
# Bypass
# ---------------------------------------------------------------------------


def _bypass_active() -> bool:
    return os.environ.get(_BYPASS_ENV_VAR, "") == _BYPASS_SECRET


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def login(token: str) -> License:
    """Verify a JWT and persist it to disk for future ensure_license calls."""
    token = token.strip()
    if not token:
        raise LicenseError("no license token provided")
    claims = _verify_token(token)
    license_obj = _license_from_claims(token, claims)
    _save_license_token(token)

    state = _load_state()
    _refresh_revocations_opportunistic(state)
    if state.last_online_at is None:
        state.last_online_at = datetime.now(tz=UTC)
        _save_state(state)

    return license_obj


def logout() -> bool:
    """Delete the stored license file. Return True if a file was removed."""
    path = _license_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def whoami() -> License | None:
    """Return the currently loaded license, or None if none is installed.

    Raises LicenseError if a license file exists but is corrupt or
    invalid — callers that want a silent probe should catch it.
    """
    return _load_license()


def ensure_license(*, feature: str) -> License | None:
    """Gate a premium command on a valid, non-revoked license.

    Returns the verified License when the command may proceed, or None
    when the bypass env var is active. Raises LicenseError when the
    command must be blocked.

    When the license is in hard grace (15 to 60 days offline), prints a
    warning to stderr but still allows the command.
    """
    if _bypass_active():
        _emit_warning(
            f"franklin license bypass is active via {_BYPASS_ENV_VAR}; command will not be gated"
        )
        return None

    license_obj = _load_license()
    if license_obj is None:
        raise LicenseError("no franklin license installed — run `franklin license login`")

    if feature not in license_obj.features:
        raise LicenseError(
            f"license does not grant the {feature!r} feature "
            f"(granted: {', '.join(license_obj.features) or 'none'})"
        )

    state = _load_state()
    _refresh_revocations_opportunistic(state)

    if license_obj.jti is not None and license_obj.jti in state.revoked_jtis:
        raise LicenseError("license has been revoked — contact support for a replacement")

    _check_grace_window(state)
    return license_obj


def _check_grace_window(state: _LocalState) -> None:
    last = state.last_online_at
    if last is None:
        # No recorded online check — require a fresh one before gating.
        raise LicenseError(
            "cannot verify license — no successful online check yet. "
            "run `franklin license login` while connected to the internet"
        )
    days_offline = (datetime.now(tz=UTC) - last).days
    if days_offline > _GRACE_HARD_DAYS:
        raise LicenseError(
            f"license has been offline for {days_offline} days "
            f"(hard grace {_GRACE_HARD_DAYS} exceeded) — "
            "run `franklin license login` while connected to the internet"
        )
    if days_offline > _GRACE_SOFT_DAYS:
        _emit_warning(
            f"franklin license has not been verified online in "
            f"{days_offline} days; please reconnect soon"
        )


def refresh_revocations() -> bool:
    """Force a phone-home and refresh the cached revocation list.

    Returns True on a successful refresh, False if the network call
    failed or the response was unparseable. Intended for
    ``franklin license status --refresh``.
    """
    return _refresh_revocations_opportunistic(_load_state())


def status() -> LicenseStatus:
    """Report the operational health of the installed license.

    Never raises. Every failure mode (missing file, corrupt JWT, expired,
    revoked, past hard grace) is returned as a ``LicenseHealth`` value.
    Pure computation — no printing, no exit, no network. Callers render
    the returned dataclass however they like.
    """
    bypass_active = _bypass_active()

    # Try loading the license; fall back to explicit health values on
    # every recoverable failure.
    license_obj: License | None
    corrupt_detail: str | None = None
    try:
        license_obj = _load_license()
    except LicenseError as exc:
        license_obj = None
        corrupt_detail = str(exc)

    state = _load_state()

    if license_obj is None and corrupt_detail is None:
        if bypass_active:
            return LicenseStatus(
                health=LicenseHealth.BYPASS_ACTIVE,
                license=None,
                days_until_expiry=None,
                days_since_online=_days_since(state.last_online_at),
                grace_band=_grace_band_from_days(_days_since(state.last_online_at)),
                bypass_active=True,
                next_step="bypass is active via FRANKLIN_LICENSE_BYPASS; no license is installed",
                underlying_health=LicenseHealth.NO_LICENSE,
            )
        return LicenseStatus(
            health=LicenseHealth.NO_LICENSE,
            license=None,
            days_until_expiry=None,
            days_since_online=_days_since(state.last_online_at),
            grace_band="unknown",
            bypass_active=False,
            next_step="run `franklin license login` to install a license",
        )

    if license_obj is None:
        # Corrupt license file
        if bypass_active:
            return LicenseStatus(
                health=LicenseHealth.BYPASS_ACTIVE,
                license=None,
                days_until_expiry=None,
                days_since_online=_days_since(state.last_online_at),
                grace_band=_grace_band_from_days(_days_since(state.last_online_at)),
                bypass_active=True,
                next_step="bypass is active; fix the corrupt license file when you can",
                detail=corrupt_detail,
                underlying_health=LicenseHealth.CORRUPT_LICENSE,
            )
        expired = corrupt_detail is not None and "expired" in corrupt_detail.lower()
        if expired:
            return LicenseStatus(
                health=LicenseHealth.BLOCKED_EXPIRED,
                license=None,
                days_until_expiry=None,
                days_since_online=_days_since(state.last_online_at),
                grace_band=_grace_band_from_days(_days_since(state.last_online_at)),
                bypass_active=False,
                next_step="run `franklin license login` with a fresh token",
                detail=corrupt_detail,
            )
        return LicenseStatus(
            health=LicenseHealth.CORRUPT_LICENSE,
            license=None,
            days_until_expiry=None,
            days_since_online=_days_since(state.last_online_at),
            grace_band="unknown",
            bypass_active=False,
            next_step="run `franklin license login` with a fresh token",
            detail=corrupt_detail,
        )

    # Happy-ish path: license loaded and verified (not expired).
    days_until_expiry = (license_obj.expires_at - datetime.now(tz=UTC)).days
    days_since_online = _days_since(state.last_online_at)
    grace_band = _grace_band_from_days(days_since_online)

    # Evaluate underlying health ignoring bypass
    revoked = license_obj.jti is not None and license_obj.jti in state.revoked_jtis
    if revoked:
        underlying = LicenseHealth.BLOCKED_REVOKED
        next_step = "contact support for a replacement license"
    elif days_since_online is None:
        underlying = LicenseHealth.BLOCKED_NO_ONLINE_CHECK
        next_step = "run `franklin license status --refresh` while connected"
    elif days_since_online > _GRACE_HARD_DAYS:
        underlying = LicenseHealth.BLOCKED_HARD_GRACE
        next_step = "run `franklin license status --refresh` while connected"
    elif days_since_online > _GRACE_SOFT_DAYS:
        underlying = LicenseHealth.HARD_GRACE
        next_step = (
            f"license has been offline for {days_since_online} days; "
            "reconnect soon or gated commands will stop working"
        )
    else:
        underlying = LicenseHealth.VALID
        next_step = "no action required"

    if bypass_active:
        return LicenseStatus(
            health=LicenseHealth.BYPASS_ACTIVE,
            license=license_obj,
            days_until_expiry=days_until_expiry,
            days_since_online=days_since_online,
            grace_band=grace_band,
            bypass_active=True,
            next_step="bypass is active via FRANKLIN_LICENSE_BYPASS; "
            "underlying license health reported below",
            underlying_health=underlying,
        )

    return LicenseStatus(
        health=underlying,
        license=license_obj,
        days_until_expiry=days_until_expiry,
        days_since_online=days_since_online,
        grace_band=grace_band,
        bypass_active=False,
        next_step=next_step,
    )


def _days_since(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    return (datetime.now(tz=UTC) - moment).days


def _grace_band_from_days(days_since_online: int | None) -> str:
    if days_since_online is None:
        return "unknown"
    if days_since_online <= _GRACE_SOFT_DAYS:
        return "fresh"
    if days_since_online <= _GRACE_HARD_DAYS:
        return "hard"
    return "exceeded"


def _emit_warning(message: str) -> None:
    """Write a dim warning to stderr. Kept as a helper so tests can patch it."""
    import sys

    print(f"\x1b[2m⚠ {message}\x1b[0m", file=sys.stderr)
