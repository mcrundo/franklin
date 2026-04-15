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
from franklin.services.ingest import (
    CleanupStats,
    IngestInput,
    IngestResult,
    IngestService,
    MetadataConfirmHook,
)
from franklin.services.map import (
    ChapterNotFoundError,
    MapInput,
    MapResult,
    MapService,
    RunNotIngestedError,
    TargetSelection,
)
from franklin.services.plan import (
    NoSidecarsError,
    PlanAlreadyExistsError,
    PlanContext,
    PlanInput,
    PlanResult,
    PlanService,
)
from franklin.services.reduce import (
    ArtifactNotFoundError,
    NoPlanError,
    NoSidecarsForReduceError,
    ReduceContext,
    ReduceInput,
    ReduceResult,
    ReduceService,
    UnknownArtifactTypeError,
)

__all__ = [
    "ArtifactNotFoundError",
    "ChapterNotFoundError",
    "CleanupStats",
    "InfoEvent",
    "IngestInput",
    "IngestResult",
    "IngestService",
    "ItemDone",
    "ItemStart",
    "ItemStatus",
    "MapInput",
    "MapResult",
    "MapService",
    "MetadataConfirmHook",
    "NoPlanError",
    "NoSidecarsError",
    "NoSidecarsForReduceError",
    "PlanAlreadyExistsError",
    "PlanContext",
    "PlanInput",
    "PlanResult",
    "PlanService",
    "ProgressCallback",
    "ProgressEvent",
    "ReduceContext",
    "ReduceInput",
    "ReduceResult",
    "ReduceService",
    "RunNotIngestedError",
    "StageFinish",
    "StageStart",
    "TargetSelection",
    "UnknownArtifactTypeError",
    "WarningEvent",
    "progress_event_adapter",
]
