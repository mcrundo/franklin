"""Contract tests for the ProgressEvent discriminated union.

These pin the wire shape because a future SSE consumer will deserialize
these events from JSON — the ``kind`` discriminator and field names are
load-bearing.
"""

from __future__ import annotations

from franklin.services.events import (
    InfoEvent,
    ItemDone,
    ItemStart,
    StageFinish,
    StageStart,
    WarningEvent,
    progress_event_adapter,
)


def test_stage_start_round_trip() -> None:
    event = StageStart(stage="map", total=12)
    raw = progress_event_adapter.dump_python(event)
    restored = progress_event_adapter.validate_python(raw)
    assert isinstance(restored, StageStart)
    assert restored.stage == "map"
    assert restored.total == 12
    assert raw["kind"] == "stage_start"


def test_item_done_defaults_to_ok() -> None:
    event = ItemDone(stage="map", item_id="ch01")
    assert event.status == "ok"


def test_discriminator_routes_to_correct_type() -> None:
    payloads: list[dict[str, object]] = [
        {"kind": "stage_start", "stage": "ingest", "total": 3},
        {"kind": "item_start", "stage": "ingest", "item_id": "ch01"},
        {"kind": "item_done", "stage": "ingest", "item_id": "ch01", "status": "ok"},
        {"kind": "stage_finish", "stage": "ingest", "summary": "done"},
        {"kind": "warning", "stage": "ingest", "message": "heads up"},
        {"kind": "info", "stage": "ingest", "message": "fyi"},
    ]
    expected: list[type] = [
        StageStart,
        ItemStart,
        ItemDone,
        StageFinish,
        WarningEvent,
        InfoEvent,
    ]
    for payload, cls in zip(payloads, expected, strict=True):
        # ts is optional; adapter should fill it
        parsed = progress_event_adapter.validate_python(payload)
        assert isinstance(parsed, cls)
