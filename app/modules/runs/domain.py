"""Run engine value types (ADR-0011, ``contracts/mvp/v1/run-events.schema.json``)."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from app.core.ports.types import RunProfile

RunStatus = Literal[
    "running",
    "waiting_approval",
    "cancel_requested",
    "completed",
    "failed",
    "cancelled",
]
CancelReason = Literal["user", "client_disconnected", "budget_exceeded", "superseded"]

RUN_STATUSES: Final[tuple[RunStatus, ...]] = (
    "running",
    "waiting_approval",
    "cancel_requested",
    "completed",
    "failed",
    "cancelled",
)
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "failed", "cancelled"})
TERMINAL_EVENTS: Final[Mapping[str, RunStatus]] = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "run.started",
        "speech.ack",
        "message.delta",
        "tool.call.started",
        "tool.call.completed",
        "approval.requested",
        "approval.resolved",
        "device_tool.call",
        "memory.suggested",
        "job.created",
        "response.final",
        *TERMINAL_EVENTS,
    }
)
EXTERNAL_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"device_tool.call", "memory.suggested", "job.created"}
)
"""Event types other modules may publish into a live run (M16, M17, M23)."""


class RunNotFoundError(LookupError):
    """Missing or foreign run; callers must not reveal which."""


class RunFinishedError(RuntimeError):
    """An event was appended after the run's terminal event."""


class InvalidRunCursorError(ValueError):
    """Malformed cursor or a cursor of another run."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class NewRun:
    user_id: uuid.UUID
    profile: RunProfile
    input_text: str
    conversation_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    workspace_id: str | None = None
    app_client_id: str | None = None
    scopes: frozenset[str] = frozenset()
    incognito: bool = False
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: uuid.UUID
    user_id: uuid.UUID
    profile: RunProfile
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    last_sequence: int = 0
    model: str | None = None
    conversation_id: uuid.UUID | None = None
    turn_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    workspace_id: str | None = None
    app_client_id: str | None = None
    scopes: frozenset[str] = frozenset()
    incognito: bool = False
    input_text: str | None = None
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cancel_reason: str | None = None
    steps: int = 0
    tool_calls: int = 0
    usage: dict[str, Any] | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class StoredEvent:
    id: uuid.UUID
    run_id: uuid.UUID
    sequence: int
    type: str
    payload: dict[str, Any]
    at: datetime

    @property
    def cursor(self) -> str:
        return RunCursor(run_id=self.run_id, sequence=self.sequence).encode()

    @property
    def terminal(self) -> bool:
        return self.type in TERMINAL_EVENTS

    def to_wire(self) -> dict[str, Any]:
        """The ``run-events.schema.json`` envelope."""
        return {
            "event_id": str(self.id),
            "run_id": str(self.run_id),
            "sequence": self.sequence,
            "cursor": self.cursor,
            "type": self.type,
            "at": isoformat(self.at),
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class RunCursor:
    """Opaque resume position: SSE ``id`` of an event, bound to its run."""

    run_id: uuid.UUID
    sequence: int

    def encode(self) -> str:
        raw = json.dumps(
            {"r": str(self.run_id), "s": self.sequence, "v": 1},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @classmethod
    def decode(cls, cursor: str, *, run_id: uuid.UUID) -> RunCursor:
        if not cursor or len(cursor) > 512:
            raise InvalidRunCursorError("cursor length is invalid")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        except (UnicodeDecodeError, binascii.Error, json.JSONDecodeError, ValueError) as error:
            raise InvalidRunCursorError("cursor is malformed") from error
        if not isinstance(payload, dict) or set(payload) != {"r", "s", "v"} or payload["v"] != 1:
            raise InvalidRunCursorError("cursor shape is invalid")
        sequence = payload["s"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise InvalidRunCursorError("cursor sequence is invalid")
        if payload["r"] != str(run_id):
            raise InvalidRunCursorError("cursor belongs to another run")
        return cls(run_id=run_id, sequence=sequence)


def run_effects(status: RunStatus, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Run-row changes implied by appending ``event_type`` while the run is ``status``.

    Shared by every ``RunStore`` so the state machine lives in one place.
    ``cancel_requested`` is sticky until a terminal event.
    """
    effects: dict[str, Any] = {}
    sticky = status == "cancel_requested"
    if event_type == "run.started":
        effects["model"] = payload.get("model")
        if "conversation_id" in payload:
            effects["conversation_id"] = uuid.UUID(str(payload["conversation_id"]))
    elif event_type == "approval.requested" and not sticky:
        effects["status"] = "waiting_approval"
    elif event_type == "approval.resolved" and not sticky:
        effects["status"] = "running"
    elif event_type == "response.final":
        effects["response"] = dict(payload["response"])
    elif event_type == "run.completed":
        effects.update(
            status="completed",
            steps=int(payload["steps"]),
            tool_calls=int(payload["tool_calls"]),
            usage=payload.get("usage"),
        )
    elif event_type == "run.failed":
        effects.update(status="failed", error=dict(payload["error"]))
    elif event_type == "run.cancelled":
        effects.update(status="cancelled", cancel_reason=str(payload["reason"]))
    return effects
