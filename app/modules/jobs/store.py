"""PostgreSQL queue and schedule store.

Concurrency guarantees:

- ``claim`` takes queued rows with ``FOR UPDATE SKIP LOCKED`` and sets a lease in the same
  statement transaction, so two workers can never claim the same job;
- every later write by a worker (``renew``/``finish``/``retry``) is conditional on
  ``lease_owner`` and ``attempt``; a worker that lost its lease cannot overwrite the job;
- a schedule occurrence becomes at most one job (``dedupe_key`` unique) and due schedules
  are taken with ``SKIP LOCKED``, so two schedulers never fire the same occurrence.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, bindparam, cast, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.conversations.chats import BACKGROUND_TITLE, singleton_chat_id
from app.modules.conversations.models import ChatKind
from app.modules.conversations.repositories import IdempotencyRepository
from app.modules.jobs.chats import (
    delivery_channels,
    event_type_of,
    post_result_card,
    source_chat,
)
from app.modules.jobs.domain import (
    Job,
    JobError,
    NewJob,
    NewSchedule,
    Schedule,
    delivery_of,
    explicit_channels,
    isoformat,
    utc_now,
)
from app.modules.jobs.messages import InboxDraft, failure_draft, public_error
from app.modules.jobs.models import (
    ACTIVE_JOB_STATUSES,
    JobAttemptRow,
    JobRow,
    ScheduleRow,
)
from app.modules.jobs.recurrence import (
    MisfirePolicy,
    Recurrence,
    RecurrenceError,
    parse_recurrence,
    plan_fire,
)
from app.modules.notifications.content import plain, text_response
from app.modules.notifications.inbox import add_item
from app.modules.tools.models import ToolExecution

CREATE_JOB_ENDPOINT = "POST /v1/jobs"
CREATE_SCHEDULE_ENDPOINT = "POST /v1/schedules"
RUN_SCHEDULE_ENDPOINT = "POST /v1/schedules/{id}/run"
_WRITE_EVIDENCE = ("succeeded", "uncertain", "running")
_INTERNAL_TOOLS = ("notify",)
"""Inside a job ``notify`` only drafts the job's result (M74-R1): not an external write."""
MAX_NOTES = 20


def job_from_row(row: JobRow) -> Job:
    return Job(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        goal=row.goal,
        inputs=None if row.inputs is None else dict(row.inputs),
        status=row.status,
        origin_type=row.origin_type,
        origin_id=row.origin_id,
        run_id=row.run_id,
        budget=dict(row.budget),
        delivery=dict(row.delivery),
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        cancel_requested=row.cancel_requested,
        lease_owner=row.lease_owner,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        result=None if row.result is None else dict(row.result),
        error=None if row.error is None else dict(row.error),
    )


def schedule_from_row(row: ScheduleRow) -> Schedule:
    return Schedule(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        rule=row.rule,
        timezone=row.timezone,
        dtstart=row.dtstart,
        goal=row.goal,
        delivery=dict(row.delivery),
        misfire_policy=row.misfire_policy,
        catch_up_limit=row.catch_up_limit,
        status=row.status,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        created_at=row.created_at,
    )


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps({"t": isoformat(created_at), "i": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        moment = datetime.fromisoformat(str(payload["t"]).replace("Z", "+00:00"))
        return moment, uuid.UUID(str(payload["i"]))
    except (binascii.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise JobError("invalid_cursor", "cursor is malformed") from error


@dataclass(frozen=True, slots=True)
class LeaseState:
    cancel_requested: bool


@dataclass(frozen=True, slots=True)
class FiredSchedule:
    schedule_id: uuid.UUID
    jobs: tuple[uuid.UUID, ...]
    skipped: int


@dataclass(frozen=True, slots=True)
class Reclaimed:
    job: Job
    requeued: bool


def _notes_or_empty() -> Any:
    return func.coalesce(JobRow.notes, cast(literal("[]"), JSONB))


def _job_row(new: NewJob, *, now: datetime, max_attempts: int) -> dict[str, Any]:
    return {
        "id": new.id,
        "user_id": new.user_id,
        "kind": new.kind,
        "goal": new.goal,
        "inputs": new.inputs,
        "status": "queued",
        "origin_type": new.origin_type,
        "origin_id": new.origin_id[:256],
        "run_id": None,
        "budget": new.budget,
        "delivery": delivery_of(new.channels),
        "attempt": 1,
        "max_attempts": max_attempts,
        "run_after": new.run_after or now,
        "cancel_requested": False,
        "dedupe_key": new.dedupe_key,
        "created_at": now,
        "updated_at": now,
    }


class SqlJobStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
        max_attempts: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self.clock = clock
        self.max_attempts = max_attempts

    # -- jobs -------------------------------------------------------------------------

    async def create_job(
        self,
        new: NewJob,
        *,
        max_active: int | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        endpoint: str = CREATE_JOB_ENDPOINT,
    ) -> tuple[Job, bool]:
        now = self.clock()
        async with self._session_factory() as session, session.begin():
            claim = None
            if idempotency_key is not None:
                claim = await IdempotencyRepository(session).claim(
                    user_id=new.user_id,
                    endpoint=endpoint,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash or "",
                )
                if claim.replayed:
                    existing = (
                        await session.get(JobRow, claim.record.resource_id)
                        if claim.record.resource_id is not None
                        else None
                    )
                    if existing is None:
                        raise JobError("idempotency_conflict")
                    return job_from_row(existing), True
            if max_active is not None:
                active = await session.scalar(
                    select(func.count())
                    .select_from(JobRow)
                    .where(JobRow.user_id == new.user_id, JobRow.status.in_(ACTIVE_JOB_STATUSES))
                )
                if int(active or 0) >= max_active:
                    raise JobError("job_limit_exceeded")
            row = JobRow(**_job_row(new, now=now, max_attempts=self.max_attempts))
            session.add(row)
            await session.flush()
            if claim is not None:
                await IdempotencyRepository(session).bind_resource(
                    claim.record, resource_kind="job", resource_id=row.id
                )
            return job_from_row(row), False

    async def get_job(self, job_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> Job | None:
        async with self._session_factory() as session:
            row = await session.get(JobRow, job_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return None
        return job_from_row(row)

    async def background_conversation(self, user_id: uuid.UUID) -> uuid.UUID:
        """The hidden conversation job runs read and write (never a listed chat, M74)."""
        async with self._session_factory() as session, session.begin():
            return await singleton_chat_id(session, user_id, ChatKind.BACKGROUND, BACKGROUND_TITLE)

    async def job_for_run(self, run_id: uuid.UUID) -> Job | None:
        async with self._session_factory() as session:
            row = await session.scalar(select(JobRow).where(JobRow.run_id == run_id).limit(1))
        return None if row is None else job_from_row(row)

    async def add_note(self, job_id: uuid.UUID, run_id: uuid.UUID, note: dict[str, Any]) -> bool:
        """Keep a ``notify`` text of the job's live run for its single result (M74-R1).

        ``False`` when the run is not the job's current run any more (the job finished,
        was retried or lost its lease) or the job already holds ``MAX_NOTES`` notes.
        """
        now = self.clock()
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.run_id == run_id,
                    JobRow.status.in_(("running", "waiting_approval")),
                    func.jsonb_array_length(_notes_or_empty()) < MAX_NOTES,
                )
                .values(
                    notes=_notes_or_empty().op("||")(bindparam("note", [note], type_=JSONB)),
                    updated_at=now,
                )
                .returning(JobRow.id)
            )
            return result.first() is not None

    async def notes(self, job_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            value = await session.scalar(select(JobRow.notes).where(JobRow.id == job_id))
        return [dict(note) for note in value or () if isinstance(note, dict)]

    async def list_jobs(
        self, *, user_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Job], str | None]:
        statement = select(JobRow).where(JobRow.user_id == user_id)
        if cursor is not None:
            moment, last_id = decode_cursor(cursor)
            statement = statement.where(
                or_(
                    JobRow.created_at < moment,
                    and_(JobRow.created_at == moment, JobRow.id < last_id),
                )
            )
        statement = statement.order_by(JobRow.created_at.desc(), JobRow.id.desc()).limit(limit + 1)
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        jobs = [job_from_row(row) for row in rows[:limit]]
        following = (
            encode_cursor(jobs[-1].created_at, jobs[-1].id) if len(rows) > limit and jobs else None
        )
        return jobs, following

    async def attempts(self, job_id: uuid.UUID) -> list[JobAttemptRow]:
        async with self._session_factory() as session:
            return list(
                (
                    await session.scalars(
                        select(JobAttemptRow)
                        .where(JobAttemptRow.job_id == job_id)
                        .order_by(JobAttemptRow.attempt)
                    )
                ).all()
            )

    async def request_cancel(self, *, user_id: uuid.UUID, job_id: uuid.UUID) -> Job | None:
        """Queued → cancelled at once; running → ``cancel_requested`` for its worker."""
        now = self.clock()
        async with self._session_factory() as session, session.begin():
            row = await session.get(JobRow, job_id, with_for_update=True)
            if row is None or row.user_id != user_id:
                return None
            if row.status == "queued":
                row.status = "cancelled"
                row.finished_at = now
                row.updated_at = now
                row.lease_owner = None
                row.lease_expires_at = None
            elif row.status in ("running", "waiting_approval") and not row.cancel_requested:
                row.cancel_requested = True
                row.updated_at = now
            return job_from_row(row)

    async def claim(self, *, worker_id: str, limit: int, lease_s: float) -> list[Job]:
        now = self.clock()
        async with self._session_factory() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(JobRow)
                        .where(
                            JobRow.status == "queued",
                            JobRow.run_after <= now,
                            JobRow.cancel_requested.is_(False),
                        )
                        .order_by(JobRow.run_after, JobRow.created_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for row in rows:
                row.status = "running"
                row.lease_owner = worker_id
                row.lease_expires_at = now + timedelta(seconds=lease_s)
                row.started_at = row.started_at or now
                row.updated_at = now
                session.add(
                    JobAttemptRow(
                        id=uuid.uuid4(),
                        job_id=row.id,
                        attempt=row.attempt,
                        worker_id=worker_id,
                        started_at=now,
                    )
                )
            await session.flush()
            return [job_from_row(row) for row in rows]

    def _owned(self, job_id: uuid.UUID, worker_id: str, attempt: int) -> Any:
        return and_(
            JobRow.id == job_id,
            JobRow.lease_owner == worker_id,
            JobRow.attempt == attempt,
            JobRow.status.in_(("running", "waiting_approval")),
        )

    async def renew(
        self,
        job: Job,
        *,
        worker_id: str,
        lease_s: float,
        status: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> LeaseState | None:
        """Extend the lease; ``None`` means the lease was lost (stop working on the job)."""
        now = self.clock()
        values: dict[str, Any] = {
            "lease_expires_at": now + timedelta(seconds=lease_s),
            "updated_at": now,
        }
        if status is not None:
            values["status"] = status
        if run_id is not None:
            values["run_id"] = run_id
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(JobRow)
                .where(self._owned(job.id, worker_id, job.attempt))
                .values(**values)
                .returning(JobRow.cancel_requested)
            )
            row = result.first()
            if row is not None and run_id is not None:
                await session.execute(
                    update(JobAttemptRow)
                    .where(JobAttemptRow.job_id == job.id, JobAttemptRow.attempt == job.attempt)
                    .values(run_id=run_id)
                )
        return None if row is None else LeaseState(cancel_requested=bool(row[0]))

    async def finish(
        self,
        job: Job,
        *,
        worker_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        inbox: InboxDraft | None = None,
    ) -> Job | None:
        """Terminal state + (optionally) its inbox item and deliveries, in one transaction."""
        now = self.clock()
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                update(JobRow)
                .where(self._owned(job.id, worker_id, job.attempt))
                .values(
                    status=status,
                    result=result,
                    error=error,
                    notes=None,
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .returning(JobRow)
            )
            if row is None:
                return None
            await self._close_attempt(session, job.id, job.attempt, status, error, now)
            if inbox is not None:
                await self._add_inbox(session, row, inbox, now)
            return job_from_row(row)

    @staticmethod
    async def _add_inbox(
        session: AsyncSession, row: JobRow, draft: InboxDraft, now: datetime
    ) -> None:
        """The job's one result: inbox item and deliveries, plus the result card in the
        source chat (M74) naming that item (M74-R1)."""
        chat = await source_chat(session, row)
        item = await add_item(
            session,
            user_id=row.user_id,
            source_type="job",
            source_id=str(row.id),
            title=draft.title,
            response=draft.response,
            channels=await delivery_channels(session, row, chat),
            now=now,
            event_type=event_type_of(row),
        )
        if chat is not None:
            await post_result_card(session, row, draft, now, chat=chat, inbox_item_id=item.id)

    async def notify_waiting_approval(self, job: Job) -> None:
        """«Approval» event (M77): a background run waits for the user's confirmation.

        Routed by the user's settings; the job's explicit result channels do not apply.
        """
        goal = plain(job.goal, 120)
        async with self._session_factory() as session, session.begin():
            await add_item(
                session,
                user_id=job.user_id,
                source_type="job",
                source_id=str(job.id),
                title="Нужно подтверждение",
                response=text_response(f"Задача «{goal}» ждёт вашего подтверждения в Mia."),
                channels=None,
                now=self.clock(),
                event_type="approval",
            )

    async def retry(
        self, job: Job, *, worker_id: str, delay_s: float, error_code: str | None
    ) -> bool:
        now = self.clock()
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(JobRow)
                .where(self._owned(job.id, worker_id, job.attempt))
                .values(
                    status="queued",
                    attempt=job.attempt + 1,
                    run_after=now + timedelta(seconds=delay_s),
                    lease_owner=None,
                    lease_expires_at=None,
                    run_id=None,
                    notes=None,
                    updated_at=now,
                )
                .returning(JobRow.id)
            )
            if result.first() is None:
                return False
            await self._close_attempt(
                session,
                job.id,
                job.attempt,
                "retry",
                {"code": error_code} if error_code else None,
                now,
            )
            return True

    async def expire_lease(self, job: Job, *, worker_id: str) -> None:
        """Hand a job back (graceful shutdown): any worker reclaims it on its next tick."""
        now = self.clock()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(JobRow)
                .where(self._owned(job.id, worker_id, job.attempt))
                .values(lease_expires_at=now - timedelta(seconds=1), updated_at=now)
            )

    @staticmethod
    async def _close_attempt(
        session: AsyncSession,
        job_id: uuid.UUID,
        attempt: int,
        outcome: str,
        error: dict[str, Any] | None,
        now: datetime,
    ) -> None:
        await session.execute(
            update(JobAttemptRow)
            .where(
                JobAttemptRow.job_id == job_id,
                JobAttemptRow.attempt == attempt,
                JobAttemptRow.finished_at.is_(None),
            )
            .values(
                finished_at=now,
                outcome=outcome,
                error_code=None if error is None else str(error.get("code"))[:64],
            )
        )

    async def reclaim_expired(
        self, *, limit: int = 20, retry_delay_s: float = 0
    ) -> list[Reclaimed]:
        """Jobs whose worker vanished: requeue (no external write happened) or fail."""
        now = self.clock()
        reclaimed: list[Reclaimed] = []
        async with self._session_factory() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(JobRow)
                        .where(
                            JobRow.status.in_(("running", "waiting_approval")),
                            JobRow.lease_expires_at < now,
                        )
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for row in rows:
                wrote = row.run_id is not None and await self._run_wrote(session, row.run_id)
                await self._close_attempt(
                    session, row.id, row.attempt, "interrupted", {"code": "job_interrupted"}, now
                )
                requeue = row.attempt < row.max_attempts and not wrote and not row.cancel_requested
                if requeue:
                    row.status = "queued"
                    row.attempt += 1
                    row.run_after = now + timedelta(seconds=retry_delay_s)
                    row.run_id = None
                elif row.cancel_requested:
                    row.status = "cancelled"
                    row.finished_at = now
                else:
                    row.status = "failed"
                    row.finished_at = now
                    row.error = public_error("job_interrupted", retryable=True)
                    await self._add_inbox(session, row, failure_draft(row.goal, row.notes), now)
                row.notes = None
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                reclaimed.append(Reclaimed(job_from_row(row), requeue))
        return reclaimed

    async def run_wrote(self, run_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            return await self._run_wrote(session, run_id)

    @staticmethod
    async def _run_wrote(session: AsyncSession, run_id: uuid.UUID) -> bool:
        """Did the run (possibly) perform a write tool call? Then it is never re-run."""
        found = await session.scalar(
            select(ToolExecution.id)
            .where(
                ToolExecution.run_id == run_id,
                ToolExecution.risk.in_(("write_reversible", "write_external")),
                ToolExecution.status.in_(_WRITE_EVIDENCE),
                ToolExecution.name.not_in(_INTERNAL_TOOLS),
            )
            .limit(1)
        )
        return found is not None

    # -- schedules -------------------------------------------------------------------

    async def create_schedule(
        self,
        new: NewSchedule,
        recurrence: Recurrence,
        *,
        max_schedules: int,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[Schedule, bool]:
        now = self.clock()
        async with self._session_factory() as session, session.begin():
            claim = None
            if idempotency_key is not None:
                claim = await IdempotencyRepository(session).claim(
                    user_id=new.user_id,
                    endpoint=CREATE_SCHEDULE_ENDPOINT,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash or "",
                )
                if claim.replayed:
                    existing = (
                        await session.get(ScheduleRow, claim.record.resource_id)
                        if claim.record.resource_id is not None
                        else None
                    )
                    if existing is None:
                        raise JobError("idempotency_conflict")
                    return schedule_from_row(existing), True
            count = await session.scalar(
                select(func.count())
                .select_from(ScheduleRow)
                .where(ScheduleRow.user_id == new.user_id)
            )
            if int(count or 0) >= max_schedules:
                raise JobError("schedule_limit_exceeded")
            row = ScheduleRow(
                id=new.id,
                user_id=new.user_id,
                kind=new.kind,
                rule=new.rule,
                timezone=new.timezone,
                dtstart=recurrence.dtstart,
                goal=new.goal,
                delivery=delivery_of(new.channels),
                misfire_policy=new.misfire_policy,
                catch_up_limit=new.catch_up_limit,
                status="active",
                next_run_at=recurrence.after(now),
                last_run_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            if claim is not None:
                await IdempotencyRepository(session).bind_resource(
                    claim.record, resource_kind="schedule", resource_id=row.id
                )
            return schedule_from_row(row), False

    async def get_schedule(self, *, user_id: uuid.UUID, schedule_id: uuid.UUID) -> Schedule | None:
        async with self._session_factory() as session:
            row = await session.get(ScheduleRow, schedule_id)
        if row is None or row.user_id != user_id:
            return None
        return schedule_from_row(row)

    async def list_schedules(self, *, user_id: uuid.UUID) -> list[Schedule]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(ScheduleRow)
                    .where(ScheduleRow.user_id == user_id)
                    .order_by(ScheduleRow.created_at, ScheduleRow.id)
                )
            ).all()
        return [schedule_from_row(row) for row in rows]

    async def update_schedule(
        self,
        *,
        user_id: uuid.UUID,
        schedule_id: uuid.UUID,
        changes: dict[str, Any],
        min_interval_s: int,
    ) -> Schedule | None:
        """Apply ``status``/``rule``/``timezone``/``goal``; a resumed or re-ruled schedule
        restarts from now (occurrences missed while paused are not misfires)."""
        now = self.clock()
        async with self._session_factory() as session, session.begin():
            row = await session.get(ScheduleRow, schedule_id, with_for_update=True)
            if row is None or row.user_id != user_id:
                return None
            recompute = False
            if "goal" in changes:
                row.goal = changes["goal"]
            if "rule" in changes or "timezone" in changes:
                rule = changes.get("rule", row.rule)
                timezone = changes.get("timezone", row.timezone)
                recurrence = parse_recurrence(
                    rule, timezone, now=now, min_interval_s=min_interval_s
                )
                row.rule, row.timezone, row.dtstart = rule, timezone, recurrence.dtstart
                recompute = True
            if "status" in changes and changes["status"] != row.status:
                row.status = changes["status"]
                recompute = recompute or row.status == "active"
            if recompute:
                recurrence = parse_recurrence(row.rule, row.timezone, now=now, dtstart=row.dtstart)
                row.next_run_at = recurrence.after(now)
            row.updated_at = now
            return schedule_from_row(row)

    async def delete_schedule(self, *, user_id: uuid.UUID, schedule_id: uuid.UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            row = await session.get(ScheduleRow, schedule_id, with_for_update=True)
            if row is None or row.user_id != user_id:
                return False
            await session.delete(row)
            return True

    async def fire_due(
        self,
        *,
        grace_s: float,
        budget_for: Callable[[str], dict[str, Any]],
        limit: int = 50,
    ) -> list[FiredSchedule]:
        """Turn due schedule occurrences into queued jobs according to the misfire policy."""
        now = self.clock()
        fired: list[FiredSchedule] = []
        async with self._session_factory() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(ScheduleRow)
                        .where(
                            ScheduleRow.status == "active",
                            ScheduleRow.next_run_at.is_not(None),
                            ScheduleRow.next_run_at <= now,
                        )
                        .order_by(ScheduleRow.next_run_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for row in rows:
                assert row.next_run_at is not None
                try:
                    recurrence = parse_recurrence(
                        row.rule, row.timezone, now=now, dtstart=row.dtstart
                    )
                except RecurrenceError:
                    # A stored rule that no longer evaluates (e.g. zone data changed): park it.
                    row.status, row.next_run_at, row.updated_at = "paused", None, now
                    continue
                policy: MisfirePolicy = row.misfire_policy  # type: ignore[assignment]
                plan = plan_fire(
                    recurrence,
                    next_run_at=row.next_run_at,
                    now=now,
                    policy=policy,
                    catch_up_limit=row.catch_up_limit,
                    grace_s=grace_s,
                )
                created: list[uuid.UUID] = []
                for occurrence, late in plan.run:
                    new = NewJob(
                        user_id=row.user_id,
                        goal=row.goal,
                        origin_type="schedule",
                        origin_id=str(row.id),
                        budget=budget_for(row.kind),
                        channels=explicit_channels(row.delivery),
                        inputs={"scheduled_for": isoformat(occurrence), "late": late},
                        kind="reminder" if row.kind == "reminder" else "run",
                        dedupe_key=f"schedule:{row.id}:{isoformat(occurrence)}",
                    )
                    inserted = await session.scalar(
                        insert(JobRow)
                        .values(**_job_row(new, now=now, max_attempts=self.max_attempts))
                        .on_conflict_do_nothing(constraint="uq_jobs_dedupe_key")
                        .returning(JobRow.id)
                    )
                    if inserted is not None:
                        created.append(inserted)
                    row.last_run_at = occurrence
                row.next_run_at = plan.next_run_at
                row.updated_at = now
                fired.append(FiredSchedule(row.id, tuple(created), plan.skipped))
        return fired

    async def schedule_job(
        self,
        *,
        user_id: uuid.UUID,
        schedule_id: uuid.UUID,
        budget_for: Callable[[str], dict[str, Any]],
        max_active: int,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> Job | None:
        """``run_now``: one extra job from the schedule; its timing is unchanged."""
        schedule = await self.get_schedule(user_id=user_id, schedule_id=schedule_id)
        if schedule is None:
            return None
        job, _ = await self.create_job(
            NewJob(
                user_id=user_id,
                goal=schedule.goal,
                origin_type="schedule",
                origin_id=str(schedule.id),
                budget=budget_for(schedule.kind),
                channels=explicit_channels(schedule.delivery),
                inputs={"scheduled_for": isoformat(self.clock()), "late": False, "manual": True},
                kind="reminder" if schedule.kind == "reminder" else "run",
            ),
            max_active=max_active,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            endpoint=RUN_SCHEDULE_ENDPOINT,
        )
        return job

    async def users_jobs(self, ids: Sequence[uuid.UUID]) -> list[Job]:
        async with self._session_factory() as session:
            rows = (await session.scalars(select(JobRow).where(JobRow.id.in_(ids)))).all()
        return [job_from_row(row) for row in rows]
