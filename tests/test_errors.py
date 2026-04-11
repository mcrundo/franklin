"""Tests for the friendly error formatter."""

from __future__ import annotations

from unittest.mock import MagicMock

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

from franklin.errors import format_friendly_error
from franklin.ingest import UnsupportedFormatError
from franklin.license import LicenseError
from franklin.secrets import MissingApiKeyError


def _mock_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    resp.request = MagicMock()
    return resp


def test_rate_limit_classified() -> None:
    exc = RateLimitError(
        message="rate limit", response=_mock_response(429), body=None
    )
    result = format_friendly_error(exc)
    assert "rate limit" in result.title.lower()
    assert result.is_retryable is True


def test_authentication_error_classified() -> None:
    exc = AuthenticationError(
        message="bad key", response=_mock_response(401), body=None
    )
    result = format_friendly_error(exc)
    assert "authentication" in result.title.lower()
    assert "franklin doctor" in result.suggestion


def test_permission_denied_classified() -> None:
    exc = PermissionDeniedError(
        message="denied", response=_mock_response(403), body=None
    )
    result = format_friendly_error(exc)
    assert "permission" in result.title.lower()


def test_529_overloaded_classified() -> None:
    exc = APIStatusError(
        message="overloaded", response=_mock_response(529), body=None
    )
    result = format_friendly_error(exc)
    assert "529" in result.title or "overloaded" in result.title.lower()
    assert result.is_retryable is True


def test_500_generic_server_error_classified() -> None:
    exc = APIStatusError(
        message="kaboom", response=_mock_response(500), body=None
    )
    result = format_friendly_error(exc)
    assert "500" in result.title
    assert result.is_retryable is True


def test_400_client_error_not_retryable() -> None:
    exc = APIStatusError(
        message="bad shape", response=_mock_response(400), body=None
    )
    result = format_friendly_error(exc)
    assert result.is_retryable is False


def test_connection_error_classified() -> None:
    exc = APIConnectionError(request=MagicMock())
    result = format_friendly_error(exc)
    assert "network" in result.title.lower()
    assert result.is_retryable is True


def test_timeout_classified() -> None:
    exc = APITimeoutError(request=MagicMock())
    result = format_friendly_error(exc)
    assert "timed out" in result.title.lower()
    assert result.is_retryable is True


def test_missing_api_key_classified() -> None:
    result = format_friendly_error(MissingApiKeyError("no key"))
    assert "api key" in result.title.lower()
    assert result.exit_code == 2


def test_license_error_classified() -> None:
    result = format_friendly_error(LicenseError("expired"))
    assert "license" in result.title.lower()
    assert "franklin license status" in result.suggestion
    assert result.exit_code == 2


def test_unsupported_format_classified() -> None:
    result = format_friendly_error(UnsupportedFormatError("bad ext"))
    assert "format" in result.title.lower()
    assert result.exit_code == 2


def test_unknown_error_falls_back_to_generic() -> None:
    result = format_friendly_error(RuntimeError("who knows"))
    assert "Unexpected" in result.title
    assert "RuntimeError" in result.title
    assert "bug report" in result.suggestion
