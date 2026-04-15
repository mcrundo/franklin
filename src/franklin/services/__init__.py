"""Stage services — pure orchestration, independent of Typer and Rich.

Each stage (ingest, map, plan, reduce, assemble) exposes a service that
takes a Pydantic input, emits ``ProgressEvent``s via a callback, and
returns a Pydantic output. The Typer commands in ``franklin.cli`` are
thin shells that build the input, subscribe to progress, and render.
"""

from franklin.services.events import (
    InfoEvent,
    ItemDone,
    ItemStart,
    ItemStatus,
    ProgressCallback,
    ProgressEvent,
    StageFinish,
    StageStart,
    WarningEvent,
    progress_event_adapter,
)

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
