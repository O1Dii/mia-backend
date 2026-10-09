"""Run engine (M12, ADR-0011): runs, append-only run events, profiles, speech/display."""

from app.modules.runs.domain import (
    EVENT_TYPES,
    EXTERNAL_EVENT_TYPES,
    TERMINAL_EVENTS,
    InvalidRunCursorError,
    NewRun,
    RunCursor,
    RunFinishedError,
    RunNotFoundError,
    RunRecord,
    RunStatus,
    StoredEvent,
)
from app.modules.runs.engine import CancelSignal, EngineConfig, RunEngine, RunFeed
from app.modules.runs.notifier import RunNotifier
from app.modules.runs.profiles import ProfileLimits, RunLimits
from app.modules.runs.response import ParsedResponse, parse_response
from app.modules.runs.service import RunService, VoiceFeedOnlyError
from app.modules.runs.store import InMemoryRunStore, RunStore, SqlRunStore

__all__ = [
    "EVENT_TYPES",
    "EXTERNAL_EVENT_TYPES",
    "TERMINAL_EVENTS",
    "CancelSignal",
    "EngineConfig",
    "InMemoryRunStore",
    "InvalidRunCursorError",
    "NewRun",
    "ParsedResponse",
    "ProfileLimits",
    "RunCursor",
    "RunEngine",
    "RunFeed",
    "RunFinishedError",
    "RunLimits",
    "RunNotFoundError",
    "RunNotifier",
    "RunRecord",
    "RunService",
    "RunStatus",
    "RunStore",
    "SqlRunStore",
    "StoredEvent",
    "VoiceFeedOnlyError",
    "parse_response",
]
