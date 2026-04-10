"""Tests for the env-first keyring-fallback secrets resolver."""

from __future__ import annotations

import pytest

from franklin import secrets
from franklin.secrets import (
    ANTHROPIC_ENV_VAR,
    KEYRING_SERVICE,
    MissingApiKeyError,
    ensure_anthropic_api_key,
    resolve_anthropic_api_key,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ANTHROPIC_API_KEY is unset at the start of each test."""
    monkeypatch.delenv(ANTHROPIC_ENV_VAR, raising=False)


def _stub_keyring(monkeypatch: pytest.MonkeyPatch, value: str | None) -> list[tuple[str, str]]:
    """Replace keyring.get_password with a stub and record its calls."""
    calls: list[tuple[str, str]] = []

    def fake_get_password(service: str, username: str) -> str | None:
        calls.append((service, username))
        return value

    monkeypatch.setattr(secrets.keyring, "get_password", fake_get_password)
    return calls


def test_env_value_takes_precedence_over_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANTHROPIC_ENV_VAR, "from-env")
    calls = _stub_keyring(monkeypatch, "from-keychain")

    assert resolve_anthropic_api_key() == "from-env"
    assert calls == []  # keyring must not be touched when env is set


def test_keyring_is_used_when_env_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_keyring(monkeypatch, "from-keychain")

    assert resolve_anthropic_api_key() == "from-keychain"
    assert calls == [(KEYRING_SERVICE, ANTHROPIC_ENV_VAR)]


def test_whitespace_env_falls_through_to_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANTHROPIC_ENV_VAR, "   ")
    _stub_keyring(monkeypatch, "from-keychain")

    assert resolve_anthropic_api_key() == "from-keychain"


def test_missing_from_both_sources_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_keyring(monkeypatch, None)
    with pytest.raises(MissingApiKeyError, match="keyring set franklin"):
        resolve_anthropic_api_key()


def test_ensure_populates_env_from_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_keyring(monkeypatch, "from-keychain")
    ensure_anthropic_api_key()
    import os

    assert os.environ[ANTHROPIC_ENV_VAR] == "from-keychain"


def test_ensure_noop_when_env_already_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANTHROPIC_ENV_VAR, "already-here")
    calls = _stub_keyring(monkeypatch, "should-not-be-used")

    ensure_anthropic_api_key()

    import os

    assert os.environ[ANTHROPIC_ENV_VAR] == "already-here"
    assert calls == []


def test_ensure_raises_missing_when_neither_source_has_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_keyring(monkeypatch, None)
    with pytest.raises(MissingApiKeyError):
        ensure_anthropic_api_key()


def test_keyring_empty_string_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_keyring(monkeypatch, "   ")
    with pytest.raises(MissingApiKeyError):
        resolve_anthropic_api_key()
