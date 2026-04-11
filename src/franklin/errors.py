"""Friendly error formatter for CLI-facing failures.

Turns raw exceptions — especially Anthropic SDK errors and common
network failures — into a structured ``FriendlyError`` with a title,
detail, and one-line suggested next step. The CLI layer catches the
dataclass and renders it; tests can assert against the structured
values without parsing Rich markup.

Everything here is best-effort: unrecognized errors fall through to a
generic "unexpected error" message with the exception's class name,
which is strictly better than a raw traceback but still pastes
cleanly into a bug report.
"""

from __future__ import annotations

from dataclasses import dataclass

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

from franklin.ingest import UnsupportedFormatError
from franklin.license import LicenseError
from franklin.secrets import MissingApiKeyError


@dataclass(frozen=True)
class FriendlyError:
    """One user-facing error, ready for the CLI to render."""

    title: str
    detail: str
    suggestion: str
    exit_code: int = 1
    is_retryable: bool = False


def format_friendly_error(exc: BaseException) -> FriendlyError:
    """Classify an exception and return a FriendlyError.

    Recognized shapes in order of specificity:

    1. Anthropic SDK errors (rate limits, auth, overload, network, timeout)
    2. Franklin-specific errors (missing key, license, unsupported format)
    3. Everything else → generic fallback
    """
    # ---- Anthropic SDK ----
    if isinstance(exc, RateLimitError):
        return FriendlyError(
            title="Anthropic rate limit hit",
            detail=str(exc),
            suggestion=(
                "wait 30-60s and re-run the same command; "
                "franklin will resume from the stage that failed"
            ),
            is_retryable=True,
        )
    if isinstance(exc, AuthenticationError):
        return FriendlyError(
            title="Anthropic authentication failed",
            detail=str(exc),
            suggestion=(
                "your ANTHROPIC_API_KEY is invalid or expired — "
                "run `franklin doctor` to check key resolution"
            ),
        )
    if isinstance(exc, PermissionDeniedError):
        return FriendlyError(
            title="Anthropic permission denied",
            detail=str(exc),
            suggestion=(
                "your API key is valid but not authorized for the requested model "
                "or feature — check your org's console.anthropic.com access"
            ),
        )
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status == 529:
            return FriendlyError(
                title="Anthropic is overloaded (HTTP 529)",
                detail=str(exc),
                suggestion=(
                    "Anthropic's servers are temporarily overloaded; "
                    "retry in 60s and franklin will resume from the failed stage"
                ),
                is_retryable=True,
            )
        if status and 500 <= status < 600:
            return FriendlyError(
                title=f"Anthropic server error (HTTP {status})",
                detail=str(exc),
                suggestion="retry in a minute; the same command resumes where it stopped",
                is_retryable=True,
            )
        return FriendlyError(
            title=f"Anthropic API error (HTTP {status})",
            detail=str(exc),
            suggestion="check the detail above; this is usually a request-shape bug",
        )
    if isinstance(exc, APITimeoutError):
        return FriendlyError(
            title="Anthropic request timed out",
            detail=str(exc),
            suggestion="retry the command; timeouts usually clear on a second attempt",
            is_retryable=True,
        )
    if isinstance(exc, APIConnectionError):
        return FriendlyError(
            title="Network connection to Anthropic failed",
            detail=str(exc),
            suggestion=(
                "check your internet connection and firewall; "
                "`franklin doctor` includes a reachability probe"
            ),
            is_retryable=True,
        )

    # ---- Franklin-specific ----
    if isinstance(exc, MissingApiKeyError):
        return FriendlyError(
            title="No Anthropic API key configured",
            detail=str(exc),
            suggestion="run `franklin doctor` for a full diagnosis and setup hints",
            exit_code=2,
        )
    if isinstance(exc, LicenseError):
        return FriendlyError(
            title="License check failed",
            detail=str(exc),
            suggestion="run `franklin license status` for a full diagnosis",
            exit_code=2,
        )
    if isinstance(exc, UnsupportedFormatError):
        return FriendlyError(
            title="Unsupported book format",
            detail=str(exc),
            suggestion="franklin accepts .epub or .pdf — convert the source file first",
            exit_code=2,
        )

    # ---- Generic fallback ----
    return FriendlyError(
        title=f"Unexpected error: {type(exc).__name__}",
        detail=str(exc) or "(no message)",
        suggestion=(
            "re-run with --force to retry from scratch, or paste this message "
            "into a bug report with `franklin doctor --json`"
        ),
    )
