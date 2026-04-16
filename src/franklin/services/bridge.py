"""Async bridge for streaming ProgressEvents to SSE / WebSocket consumers.

Services emit progress via a sync ``ProgressCallback``. Web handlers
need an async iterator of events to stream as SSE frames. This module
bridges the two with a ``ProgressBridge`` backed by ``asyncio.Queue``.

Intended usage with the async service variants from RUB-109::

    bridge = ProgressBridge()

    async def background():
        try:
            await MapService().run_async(params, progress=bridge.push)
        finally:
            bridge.close()

    task = asyncio.create_task(background())

    async for event in bridge.events():
        yield f"data: {progress_event_adapter.dump_json(event).decode()}\\n\\n"

The bridge assumes producer and consumer share the same event loop
(which is the case when using ``run_async`` inside an ``asyncio.Task``
alongside the SSE generator). For the sync ``run()`` path running in
a thread via ``run_in_executor``, use ``ProgressBridgeThreadSafe``
which marshals events across threads.
"""

from __future__ import annotations

import asyncio
import queue
from collections.abc import AsyncIterator
from typing import Any

from franklin.services.events import ProgressCallback, ProgressEvent


class ProgressBridge:
    """Same-loop bridge: sync push → async drain via ``asyncio.Queue``.

    Use when the service runs as an ``asyncio.Task`` in the same loop
    as the SSE generator (the recommended pattern with ``run_async``).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()

    @property
    def push(self) -> ProgressCallback:
        """The sync callback to pass as ``progress=bridge.push``."""
        return self._push

    def _push(self, event: Any) -> None:
        self._queue.put_nowait(event)

    def close(self) -> None:
        """Signal no more events. ``events()`` will stop iterating."""
        self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[ProgressEvent]:
        """Yield events as they arrive. Stops when ``close()`` is called."""
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class ProgressBridgeThreadSafe:
    """Cross-thread bridge: sync push from a worker thread → async drain.

    Use when the service runs via ``loop.run_in_executor`` (sync
    ``run()`` in a thread pool) and the consumer is an async SSE
    generator in the main loop. The push side uses a ``queue.Queue``
    (thread-safe); the async drain polls it.
    """

    def __init__(self, *, poll_interval: float = 0.05) -> None:
        self._queue: queue.Queue[ProgressEvent | None] = queue.Queue()
        self._poll_interval = poll_interval

    @property
    def push(self) -> ProgressCallback:
        """The sync callback — safe to call from any thread."""
        return self._push

    def _push(self, event: Any) -> None:
        self._queue.put_nowait(event)

    def close(self) -> None:
        """Signal no more events."""
        self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[ProgressEvent]:
        """Yield events, polling the thread-safe queue.

        Uses a short sleep between polls to avoid busy-waiting while
        still delivering events with low latency (~50ms default).
        """
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(self._poll_interval)
                continue
            if event is None:
                break
            yield event


__all__ = [
    "ProgressBridge",
    "ProgressBridgeThreadSafe",
]
