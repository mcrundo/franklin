"""Progress events emitted by stage services.

Services publish these to a callback; the CLI subscribes and renders to
Rich. The event shape is a Pydantic discriminated union keyed on
``kind`` so it round-trips through JSON for a future SSE consumer
without changing the producer side.

Event vocabulary, in the order a typical stage emits them:

    stage_start   → once, with an optional total (for progress bars)
    item_start    → per work item, before heavy work begins
    item_done     → per work item, with ok/skip/fail status
    stage_finish  → once, with a short human summary

``warning`` and ``info`` are out-of-band and can fire at any point.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ItemStatus = Literal["ok", "skip", "fail"]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    stage: str
    ts: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class StageStart(_Event):
    kind: Literal["stage_start"] = "stage_start"
    total: int | None = None


class StageFinish(_Event):
    kind: Literal["stage_finish"] = "stage_finish"
    summary: str | None = None


class ItemStart(_Event):
    kind: Literal["item_start"] = "item_start"
    item_id: str
    label: str | None = None


class ItemDone(_Event):
    kind: Literal["item_done"] = "item_done"
    item_id: str
    status: ItemStatus = "ok"
    detail: str | None = None


class WarningEvent(_Event):
    kind: Literal["warning"] = "warning"
    message: str


class InfoEvent(_Event):
    kind: Literal["info"] = "info"
    message: str


ProgressEvent = Annotated[
    StageStart | StageFinish | ItemStart | ItemDone | WarningEvent | InfoEvent,
    Field(discriminator="kind"),
]

ProgressCallback = Callable[[ProgressEvent], None]

progress_event_adapter: TypeAdapter[ProgressEvent] = TypeAdapter(ProgressEvent)


__all__ = [
    "InfoEvent",
    "ItemDone",
    "ItemStart",
    "ItemStatus",
    "ProgressCallback",
    "ProgressEvent",
    "StageFinish",
    "StageStart",
    "WarningEvent",
    "progress_event_adapter",
]
