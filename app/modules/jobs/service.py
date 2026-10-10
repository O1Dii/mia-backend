"""Jobs and schedules use cases shared by the HTTP API and the model's core tools."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from app.modules.jobs.domain import Job, JobError, NewJob, NewSchedule, OriginType, Schedule
from app.modules.jobs.recurrence import RecurrenceError, parse_recurrence
from app.modules.jobs.settings import JobsSettings
from app.modules.jobs.store import SqlJobStore
from app.modules.notifications.content import plain, text_response, title
from app.modules.notifications.inbox import InboxItem, InboxService, SourceType
from app.modules.notifications.preferences import EventType
from app.modules.runs.domain import RunFinishedError, RunNotFoundError
from app.modules.runs.service import RunService

logger = logging.getLogger(__name__)

ALLOWED_CHANNELS = ("push", "telegram", "inbox")


def channels_of(delivery: dict[str, Any] | None) -> tuple[str, ...] | None:
    """The channels a request asked for explicitly; ``None`` = the user's notification settings
    decide when the result is delivered (M77)."""
    if not delivery or not delivery.get("channels"):
        return None
    picked = tuple(dict.fromkeys(str(c) for c in delivery["channels"] if c in ALLOWED_CHANNELS))
    return picked or None


class JobService:
    def __init__(
        self,
        store: SqlJobStore,
        inbox: InboxService,
        settings: JobsSettings,
        *,
        runs: RunService | None = None,
        job_max_steps: int = 40,
        job_timeout_s: float = 1800.0,
    ) -> None:
        self.store = store
        self.inbox = inbox
        self.settings = settings
        self.runs = runs
        self._max_steps = job_max_steps
        self._timeout_s = int(min(job_timeout_s, 1800))

    def default_budget(self, kind: str = "run") -> dict[str, Any]:
        if kind == "reminder":
            return {"max_steps": 1, "max_duration_seconds": 60}
        return {"max_steps": self._max_steps, "max_duration_seconds": self._timeout_s}

    def _budget(self, requested: dict[str, Any] | None) -> dict[str, Any]:
        """A requested budget may only tighten the ``job`` profile limits."""
        budget = self.default_budget()
        if requested:
            budget["max_steps"] = min(int(requested["max_steps"]), self._max_steps)
            budget["max_duration_seconds"] = min(
                int(requested["max_duration_seconds"]), self._timeout_s
            )
            if requested.get("max_cost_usd") is not None:
                budget["max_cost_usd"] = float(requested["max_cost_usd"])
        return budget

    # -- jobs --------------------------------------------------------------------------

    async def create_job(
        self,
        *,
        user_id: uuid.UUID,
        request: dict[str, Any],
        origin_type: OriginType,
        origin_id: str,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[Job, bool]:
        """``request`` is a validated ``CreateJobRequest``."""
        return await self.store.create_job(
            NewJob(
                user_id=user_id,
                goal=str(request["goal"]),
                origin_type=origin_type,
                origin_id=origin_id,
                budget=self._budget(request.get("budget")),
                channels=channels_of(request.get("delivery")),
                inputs=dict(request["inputs"]) if request.get("inputs") is not None else None,
            ),
            max_active=self.settings.jobs_max_active_per_user,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def get_job(self, *, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
        job = await self.store.get_job(job_id, user_id=user_id)
        if job is None:
            raise JobError("job_not_found")
        return job

    async def list_jobs(
        self, *, user_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Job], str | None]:
        return await self.store.list_jobs(user_id=user_id, limit=limit, cursor=cursor)

    async def cancel_job(self, *, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
        """Queued jobs are cancelled at once; a running job's run gets a cancel request
        (its worker also polls ``cancel_requested`` on every lease renewal)."""
        job = await self.store.request_cancel(user_id=user_id, job_id=job_id)
        if job is None:
            raise JobError("job_not_found")
        if job.cancel_requested and job.run_id is not None and self.runs is not None:
            try:
                await self.runs.cancel(user_id=user_id, run_id=job.run_id)
            except (RunNotFoundError, RunFinishedError):
                pass
        return job

    # -- schedules ---------------------------------------------------------------------

    def _recurrence_error(self, error: RecurrenceError) -> JobError:
        return JobError(error.code, str(error))

    async def create_schedule(
        self,
        *,
        user_id: uuid.UUID,
        request: dict[str, Any],
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[Schedule, bool]:
        """``request`` is a validated ``CreateScheduleRequest``."""
        try:
            recurrence = parse_recurrence(
                str(request["rule"]),
                str(request["timezone"]),
                now=self.store.clock(),
                min_interval_s=self.settings.jobs_min_schedule_interval_seconds,
            )
        except RecurrenceError as error:
            raise self._recurrence_error(error) from None
        return await self.store.create_schedule(
            NewSchedule(
                user_id=user_id,
                rule=str(request["rule"]),
                timezone=str(request["timezone"]),
                goal=str(request["goal"]),
                channels=channels_of(request.get("delivery")),
                misfire_policy=str(request.get("misfire_policy") or "skip"),
                catch_up_limit=int(request.get("catch_up_limit") or 3),
                kind="reminder" if request.get("kind") == "reminder" else "run",
            ),
            recurrence,
            max_schedules=self.settings.jobs_max_schedules_per_user,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def list_schedules(self, *, user_id: uuid.UUID) -> list[Schedule]:
        return await self.store.list_schedules(user_id=user_id)

    async def get_schedule(self, *, user_id: uuid.UUID, schedule_id: uuid.UUID) -> Schedule:
        schedule = await self.store.get_schedule(user_id=user_id, schedule_id=schedule_id)
        if schedule is None:
            raise JobError("schedule_not_found")
        return schedule

    async def update_schedule(
        self, *, user_id: uuid.UUID, schedule_id: uuid.UUID, changes: dict[str, Any]
    ) -> Schedule:
        try:
            schedule = await self.store.update_schedule(
                user_id=user_id,
                schedule_id=schedule_id,
                changes=changes,
                min_interval_s=self.settings.jobs_min_schedule_interval_seconds,
            )
        except RecurrenceError as error:
            raise self._recurrence_error(error) from None
        if schedule is None:
            raise JobError("schedule_not_found")
        return schedule

    async def delete_schedule(self, *, user_id: uuid.UUID, schedule_id: uuid.UUID) -> None:
        if not await self.store.delete_schedule(user_id=user_id, schedule_id=schedule_id):
            raise JobError("schedule_not_found")

    async def run_schedule_now(
        self,
        *,
        user_id: uuid.UUID,
        schedule_id: uuid.UUID,
        idempotency_key: str | None = None,
    ) -> Job:
        job = await self.store.schedule_job(
            user_id=user_id,
            schedule_id=schedule_id,
            budget_for=self.default_budget,
            max_active=self.settings.jobs_max_active_per_user,
            idempotency_key=idempotency_key,
            request_hash=str(schedule_id),
        )
        if job is None:
            raise JobError("schedule_not_found")
        return job

    # -- notify ------------------------------------------------------------------------

    async def notify(
        self,
        *,
        user_id: uuid.UUID,
        text: str,
        channels: Sequence[str] | None = None,
        heading: str | None = None,
        source_type: SourceType = "system",
        source_id: str = "notify",
        event_type: EventType = "plugin_message",
    ) -> InboxItem:
        since = self.store.clock() - timedelta(hours=1)
        if await self.inbox.count_since(user_id=user_id, since=since) >= (
            self.settings.notify_max_per_hour
        ):
            raise JobError("notify_rate_limited")
        picked = channels_of({"channels": list(channels)} if channels else None)
        return await self.inbox.create(
            user_id=user_id,
            source_type=source_type,
            source_id=source_id,
            title=title(heading or plain(text, 80)),
            response=text_response(text, text),
            channels=picked,
            event_type=event_type,
        )
