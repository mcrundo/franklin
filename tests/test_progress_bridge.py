"""Tests for ProgressBridge and ProgressBridgeThreadSafe (RUB-110).

Verifies both same-loop and cross-thread bridging patterns work:
push events from a sync callback, drain them from an async consumer.
"""

from __future__ import annotations

import asyncio

from franklin.services.bridge import ProgressBridge, ProgressBridgeThreadSafe
from franklin.services.events import InfoEvent, StageFinish, StageStart


def test_bridge_same_loop_delivers_events() -> None:
    """Push from an asyncio.Task, drain from an async-for in the same loop."""

    async def go() -> None:
        bridge = ProgressBridge()
        collected: list[str] = []

        async def producer() -> None:
            bridge.push(StageStart(stage="map", total=2))
            bridge.push(InfoEvent(stage="map", message="hello"))
            bridge.push(StageFinish(stage="map", summary="done"))
            bridge.close()

        task = asyncio.create_task(producer())

        async for event in bridge.events():
            collected.append(type(event).__name__)

        await task
        assert collected == ["StageStart", "InfoEvent", "StageFinish"]

    asyncio.run(go())


def test_bridge_same_loop_stops_on_close() -> None:
    """close() causes events() to terminate cleanly."""

    async def go() -> None:
        bridge = ProgressBridge()
        bridge.push(StageStart(stage="x"))
        bridge.close()

        count = 0
        async for _event in bridge.events():
            count += 1

        assert count == 1  # got the one event, then stopped

    asyncio.run(go())


def test_bridge_thread_safe_delivers_across_threads() -> None:
    """Push from a worker thread, drain from the main loop."""

    async def go() -> None:
        bridge = ProgressBridgeThreadSafe(poll_interval=0.01)
        collected: list[str] = []

        def sync_producer() -> None:
            bridge.push(StageStart(stage="reduce", total=1))
            bridge.push(StageFinish(stage="reduce", summary="ok"))
            bridge.close()

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, sync_producer)

        async for event in bridge.events():
            collected.append(type(event).__name__)

        assert collected == ["StageStart", "StageFinish"]

    asyncio.run(go())


def test_bridge_push_is_valid_progress_callback() -> None:
    """bridge.push satisfies the ProgressCallback type."""

    async def go() -> None:
        bridge = ProgressBridge()

        # Simulate what a service does: call progress(event)
        callback = bridge.push
        callback(StageStart(stage="test"))
        bridge.close()

        events = [e async for e in bridge.events()]
        assert len(events) == 1
        assert isinstance(events[0], StageStart)

    asyncio.run(go())
