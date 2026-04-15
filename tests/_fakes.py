"""Shared fake Anthropic clients for stage + service tests.

Every test file that exercises an LLM-backed stage used to ship its
own ``_FakeClient`` / ``_FakeAsyncClient`` / ``_FakeStream`` trio.
Those were near-identical — the SDK shape doesn't vary by caller —
so they live here now.

Three flavors cover every current caller:

- ``FakeClient`` / ``FakeAsyncClient`` — one payload for every call
  (the common case). The payload can be a literal dict or a callable
  that inspects the streamed kwargs to tailor output.
- ``ScriptedClient`` — dispatch by ``tool_name``. Used by tests that
  thread multiple stages through one client (``test_golden_path``).

All three report the same usage shape the real SDK does
(``input_tokens`` / ``output_tokens`` / cache-hit counts). Tests that
assert on specific token counts override via ``usage=``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any

PayloadFactory = Callable[[dict[str, Any]], dict[str, Any]]
PayloadOrFactory = dict[str, Any] | PayloadFactory

DEFAULT_USAGE: dict[str, int] = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}


def _response(payload: dict[str, Any], usage: dict[str, int]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input=payload)],
        stop_reason="tool_use",
        usage=SimpleNamespace(**usage),
    )


def _resolve(source: PayloadOrFactory, kwargs: dict[str, Any]) -> dict[str, Any]:
    return source(kwargs) if callable(source) else source


class FakeStream:
    """Sync context-manager stream mirroring anthropic's stream object."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._response


class FakeAsyncStream:
    """Async counterpart of ``FakeStream``."""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aenter__(self) -> FakeAsyncStream:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get_final_message(self) -> Any:
        return self._response


class FakeClient:
    """Sync fake. Every ``messages.stream`` call yields the same payload.

    Pass a plain dict for fixed output, or a callable ``(kwargs) -> dict``
    to vary by request (e.g. scrape the chapter id out of the user
    prompt).
    """

    def __init__(
        self,
        payload: PayloadOrFactory,
        *,
        usage: dict[str, int] | None = None,
    ) -> None:
        self._payload = payload
        self._usage = usage if usage is not None else DEFAULT_USAGE
        self.messages = self
        self.calls: list[dict[str, Any]] = []
        # Kept for tests that assert on the most recent call only.
        self.last_kwargs: dict[str, Any] | None = None

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        self.last_kwargs = kwargs
        return FakeStream(_response(_resolve(self._payload, kwargs), self._usage))


class FakeAsyncClient:
    """Async counterpart of ``FakeClient``."""

    def __init__(
        self,
        payload: PayloadOrFactory,
        *,
        usage: dict[str, int] | None = None,
    ) -> None:
        self._payload = payload
        self._usage = usage if usage is not None else DEFAULT_USAGE
        self.messages = self
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeAsyncStream:
        self.calls.append(kwargs)
        return FakeAsyncStream(_response(_resolve(self._payload, kwargs), self._usage))


class ScriptedClient:
    """Dispatches by ``tool_name`` — for tests that thread multiple stages.

    Each stage's ``call_tool`` selects a tool via ``tool_choice``; this
    fake looks at that field and invokes the factory registered for
    that tool name. Used by the golden-path e2e test.
    """

    def __init__(self, *, usage: dict[str, int] | None = None) -> None:
        self._handlers: dict[str, PayloadFactory] = {}
        self._usage = usage if usage is not None else DEFAULT_USAGE
        self.messages = self
        self.calls: list[dict[str, Any]] = []

    def register(self, tool_name: str, factory: PayloadFactory) -> None:
        self._handlers[tool_name] = factory

    @contextmanager
    def stream(self, **kwargs: Any) -> Iterator[FakeStream]:
        self.calls.append(kwargs)
        tool_name = kwargs["tool_choice"]["name"]
        payload = self._handlers[tool_name](kwargs)
        yield FakeStream(_response(payload, self._usage))


class ScriptedAsyncClient:
    """Async counterpart of ``ScriptedClient`` for async-stage tests."""

    def __init__(self, *, usage: dict[str, int] | None = None) -> None:
        self._handlers: dict[str, PayloadFactory] = {}
        self._usage = usage if usage is not None else DEFAULT_USAGE
        self.messages = self
        self.calls: list[dict[str, Any]] = []

    def register(self, tool_name: str, factory: PayloadFactory) -> None:
        self._handlers[tool_name] = factory

    @asynccontextmanager
    async def stream(self, **kwargs: Any) -> Any:  # AsyncIterator[FakeAsyncStream]
        self.calls.append(kwargs)
        tool_name = kwargs["tool_choice"]["name"]
        payload = self._handlers[tool_name](kwargs)
        yield FakeAsyncStream(_response(payload, self._usage))


__all__ = [
    "DEFAULT_USAGE",
    "FakeAsyncClient",
    "FakeAsyncStream",
    "FakeClient",
    "FakeStream",
    "PayloadFactory",
    "PayloadOrFactory",
    "ScriptedAsyncClient",
    "ScriptedClient",
]
