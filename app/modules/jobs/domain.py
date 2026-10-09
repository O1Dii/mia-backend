"""Job and schedule value types and their ``job.schema.json`` wire form."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

JobStatus = Literal["queued", "running", "waiting_approval", "succeeded", "failed", "cancelled"]
JobKind = Literal["run", "reminder"]
OriginType = Literal["run", "schedule", "app", "user"]
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else isoformat(value)


class JobError(Exception):
    """A public, stable ``code`` (``contracts/mvp/v1/error-codes.json``, owner M16)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class NewJob:
    user_id: uuid.UUID
    goal: str
    origin_type: OriginType
    origin_id: str
    budget: dict[str, Any]
    channels: tuple[str, ...]
    inputs: dict[str, Any] | None = None
    kind: JobKind = "run"
    run_after: datetime | None = None
    dedupe_key: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(frozen=True, slots=True)
class Job:
    id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    goal: str
    inputs: dict[str, Any] | None
    status: str
    origin_type: str
    origin_id: str
    run_id: uuid.UUID | None
    budget: dict[str, Any]
    delivery: dict[str, Any]
    attempt: int
    max_attempts: int
    cancel_requested: bool
    lease_owner: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES

    def to_wire(self) -> dict[str, Any]:
        """``job.schema.json#/$defs/Job``."""
        body: dict[str, Any] = {
            "id": str(self.id),
            "goal": self.goal,
            "status": self.status,
            "origin": {"type": self.origin_type, "id": self.origin_id},
            "run_id": None if self.run_id is None else str(self.run_id),
            "budget": self.budget,
            "delivery": self.delivery,
            "attempt": self.attempt,
            "created_at": isoformat(self.created_at),
            "started_at": _iso_or_none(self.started_at),
            "finished_at": _iso_or_none(self.finished_at),
            "result": self.result,
            "error": self.error,
        }
        if self.inputs is not None:
            body["inputs"] = self.inputs
        if self.kind != "run":
            body["kind"] = self.kind
        return body


@dataclass(frozen=True, slots=True)
class NewSchedule:
    user_id: uuid.UUID
    rule: str
    timezone: str
    goal: str
    channels: tuple[str, ...]
    misfire_policy: str = "skip"
    catch_up_limit: int = 3
    kind: JobKind = "run"
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(frozen=True, slots=True)
class Schedule:
    id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    rule: str
    timezone: str
    dtstart: datetime
    goal: str
    delivery: dict[str, Any]
    misfire_policy: str
    catch_up_limit: int
    status: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime

    def to_wire(self) -> dict[str, Any]:
        """``job.schema.json#/$defs/Schedule``."""
        return {
            "id": str(self.id),
            "rule": self.rule,
            "timezone": self.timezone,
            "goal": self.goal,
            "delivery": self.delivery,
            "misfire_policy": self.misfire_policy,
            "catch_up_limit": self.catch_up_limit,
            "kind": self.kind,
            "status": self.status,
            "next_run_at": _iso_or_none(self.next_run_at),
            "last_run_at": _iso_or_none(self.last_run_at),
            "created_at": isoformat(self.created_at),
        }
