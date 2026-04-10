"""API key resolution.

Follows an env-first, keyring-fallback pattern so the same code works
across environments without branching:

- **CI or direnv** → ANTHROPIC_API_KEY is already set in the environment
- **1Password** → `op run -- franklin map ...` injects the key into env
- **Local dev** → the key lives in the OS keychain (macOS Keychain,
  Windows Credential Manager, Linux Secret Service), pulled on demand

The Anthropic SDK reads ANTHROPIC_API_KEY from the environment, so
ensure_anthropic_api_key() just needs to populate it — every downstream
caller then works with no changes.
"""

from __future__ import annotations

import os

import keyring

ANTHROPIC_ENV_VAR = "ANTHROPIC_API_KEY"
KEYRING_SERVICE = "franklin"


class MissingApiKeyError(RuntimeError):
    """Raised when no Anthropic API key is available from env or keyring."""


def resolve_anthropic_api_key() -> str:
    """Return the Anthropic API key, checking env first then the keychain.

    Raises MissingApiKeyError with a helpful message if neither source
    yields a key.
    """
    env_value = os.environ.get(ANTHROPIC_ENV_VAR, "").strip()
    if env_value:
        return env_value

    stored = keyring.get_password(KEYRING_SERVICE, ANTHROPIC_ENV_VAR)
    if stored and stored.strip():
        return stored.strip()

    raise MissingApiKeyError(
        f"No Anthropic API key found. Either set the {ANTHROPIC_ENV_VAR} "
        f"environment variable, or store one in your OS keychain with:\n"
        f"  keyring set {KEYRING_SERVICE} {ANTHROPIC_ENV_VAR}"
    )


def ensure_anthropic_api_key() -> None:
    """Make sure ANTHROPIC_API_KEY is set in the environment.

    If the env var is already set with a non-empty value, do nothing.
    Otherwise read from the keychain and populate it so the Anthropic SDK
    picks it up on its own. Raises MissingApiKeyError if neither source
    provides a key.
    """
    if os.environ.get(ANTHROPIC_ENV_VAR, "").strip():
        return
    os.environ[ANTHROPIC_ENV_VAR] = resolve_anthropic_api_key()
