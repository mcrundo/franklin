"""Preflight health check for franklin (``franklin doctor``).

Runs a list of cheap local checks and reports each as pass/warn/fail.
Designed for first-run onboarding ("why isn't franklin working?") and
support triage ("paste the output of franklin doctor"). No LLM calls,
no writes, no destructive actions.

Each check is a pure function that returns a ``CheckResult``. The CLI
layer just renders the list; that split keeps the logic testable
without touching Rich or typer.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from franklin.license import LicenseHealth
from franklin.license import status as license_status
from franklin.secrets import ANTHROPIC_ENV_VAR, resolve_anthropic_api_key

_ANTHROPIC_HOST = "api.anthropic.com"
_ANTHROPIC_PORT = 443
_NETWORK_TIMEOUT_SECONDS = 3.0
_MIN_PYTHON = (3, 12)
_DISK_WARN_BYTES = 1_000_000_000  # 1 GB


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == CheckStatus.OK


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_python_version() -> CheckResult:
    version = sys.version_info
    label = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= _MIN_PYTHON:
        return CheckResult("Python version", CheckStatus.OK, label)
    return CheckResult(
        "Python version",
        CheckStatus.FAIL,
        f"{label} (franklin requires {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+)",
    )


def _check_uv_available() -> CheckResult:
    uv_path = shutil.which("uv")
    if uv_path:
        return CheckResult("uv binary", CheckStatus.OK, uv_path)
    return CheckResult(
        "uv binary",
        CheckStatus.WARN,
        "not found on PATH — install from https://docs.astral.sh/uv/",
    )


def _check_api_key() -> CheckResult:
    try:
        key = resolve_anthropic_api_key()
    except Exception as exc:
        return CheckResult(
            "Anthropic API key",
            CheckStatus.FAIL,
            str(exc).splitlines()[0],
        )
    source = "environment" if os.environ.get(ANTHROPIC_ENV_VAR, "").strip() else "keyring"
    tail = key[-4:] if len(key) >= 4 else "****"
    return CheckResult(
        "Anthropic API key",
        CheckStatus.OK,
        f"found via {source} (ends in …{tail})",
    )


def _check_license() -> CheckResult:
    try:
        status = license_status()
    except Exception as exc:
        return CheckResult("License", CheckStatus.WARN, f"status check failed: {exc}")

    health = status.health
    if health == LicenseHealth.VALID:
        return CheckResult("License", CheckStatus.OK, "valid")
    if health == LicenseHealth.NO_LICENSE:
        return CheckResult(
            "License",
            CheckStatus.WARN,
            "no license installed (free tier features work; premium ones won't)",
        )
    if health == LicenseHealth.HARD_GRACE:
        return CheckResult("License", CheckStatus.WARN, "in hard grace window")
    if health == LicenseHealth.BYPASS_ACTIVE:
        return CheckResult("License", CheckStatus.WARN, "bypass env var is active")
    return CheckResult("License", CheckStatus.FAIL, health.value)


def _check_claude_binary() -> CheckResult:
    claude_path = shutil.which("claude")
    if claude_path:
        return CheckResult("claude CLI", CheckStatus.OK, claude_path)
    return CheckResult(
        "claude CLI",
        CheckStatus.WARN,
        "not found on PATH — `franklin install` needs it to attach plugins",
    )


def _check_network_to_anthropic() -> CheckResult:
    try:
        with socket.create_connection(
            (_ANTHROPIC_HOST, _ANTHROPIC_PORT), timeout=_NETWORK_TIMEOUT_SECONDS
        ):
            pass
    except OSError as exc:
        return CheckResult(
            "Network → api.anthropic.com",
            CheckStatus.FAIL,
            f"connect failed: {exc}",
        )
    return CheckResult(
        "Network → api.anthropic.com",
        CheckStatus.OK,
        f"tcp connect to {_ANTHROPIC_HOST}:{_ANTHROPIC_PORT} ok",
    )


def _check_disk_space() -> CheckResult:
    runs_dir = Path.cwd() / "runs"
    target = runs_dir if runs_dir.exists() else Path.cwd()
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return CheckResult("Disk space", CheckStatus.WARN, f"could not stat: {exc}")
    free_gb = usage.free / (1024**3)
    label = f"{free_gb:.1f} GB free in {target}"
    if usage.free < _DISK_WARN_BYTES:
        return CheckResult("Disk space", CheckStatus.WARN, label)
    return CheckResult("Disk space", CheckStatus.OK, label)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _check_gh_auth() -> CheckResult:
    """Check if the gh CLI is authenticated (needed for franklin publish/push)."""
    import subprocess

    gh = shutil.which("gh")
    if not gh:
        return CheckResult(
            "GitHub CLI", CheckStatus.WARN, "gh not found — franklin publish requires it"
        )
    try:
        result = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Extract the account name from stderr (gh auth status prints there)
            for line in result.stderr.splitlines():
                if "Logged in" in line:
                    return CheckResult("GitHub CLI", CheckStatus.OK, line.strip())
            return CheckResult("GitHub CLI", CheckStatus.OK, "authenticated")
        return CheckResult(
            "GitHub CLI",
            CheckStatus.WARN,
            "not authenticated — run `gh auth login` before publishing",
        )
    except (subprocess.TimeoutExpired, OSError):
        return CheckResult("GitHub CLI", CheckStatus.WARN, "could not check gh auth status")


_DEFAULT_CHECKS: tuple[Callable[[], CheckResult], ...] = (
    _check_python_version,
    _check_uv_available,
    _check_api_key,
    _check_license,
    _check_claude_binary,
    _check_gh_auth,
    _check_network_to_anthropic,
    _check_disk_space,
)


def run_checks(*, skip_network: bool = False) -> list[CheckResult]:
    """Run every preflight check and return the results in order.

    ``skip_network`` suppresses the Anthropic reachability probe, which
    is the only check that talks to the outside world. Useful in CI or
    in air-gapped environments where a network failure isn't noteworthy.
    """
    results: list[CheckResult] = []
    for check in _DEFAULT_CHECKS:
        if skip_network and check is _check_network_to_anthropic:
            continue
        results.append(check())
    return results


def has_failures(results: list[CheckResult]) -> bool:
    return any(r.status == CheckStatus.FAIL for r in results)
