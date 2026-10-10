"""Execute one claimed job: a ``profile=job`` run (or a reminder) under a renewed lease.

While the run is live the runner renews the lease every ``lease/3`` seconds, mirrors
``waiting_approval``, forwards ``cancel_requested`` to the run and enforces the job's
``max_duration_seconds``. A lost lease cancels the local run and writes nothing.
Failed runs are retried only when the error is retryable and the run performed no write
tool call (an external effect is never repeated).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from app.modules.jobs.domain import Job
from app.modules.jobs.messages import (
    failure_draft,
    job_prompt,
    public_error,
    reminder_draft,
    success_draft,
)
from app.modules.jobs.settings import JobsSettings
from app.modules.jobs.store import SqlJobStore
from app.modules.notifications.away import suppress_away_summary
from app.modules.runs.domain import NewRun, RunRecord
from app.modules.runs.service import RunService

logger = logging.getLogger(__name__)


class JobRunner:
    def __init__(
        self,
        store: SqlJobStore,
        runs: RunService,
        settings: JobsSettings,
        *,
        worker_id: str,
    ) -> None:
        self.store = store
        self.runs = runs
        self.settings = settings
        self.worker_id = worker_id
        self._runs: dict[uuid.UUID, uuid.UUID] = {}

    @property
    def lease_s(self) -> float:
        return self.settings.jobs_lease_seconds

    async def execute(self, job: Job) -> None:
        try:
            if job.kind == "reminder":
                await self.store.finish(
                    job,
                    worker_id=self.worker_id,
                    status="succeeded",
                    result=reminder_draft(job.goal).response,
                    inbox=reminder_draft(job.goal),
                )
                return
            await self._execute_run(job)
        except asyncio.CancelledError:
            # Worker shutdown: stop the run, then hand the job back for another worker.
            await asyncio.shield(self._abandon(job))
            raise
        finally:
            self._runs.pop(job.id, None)

    async def _abandon(self, job: Job) -> None:
        run_id = self._runs.get(job.id)
        if run_id is not None and self.runs.is_live(run_id):
            await self._cancel_run(job, run_id, "superseded")
            await self.runs.wait_finished(run_id)
        await self.store.expire_lease(job, worker_id=self.worker_id)

    async def _execute_run(self, job: Job) -> None:
        run_id = uuid.uuid4()
        self._runs[job.id] = run_id
        if (
            await self.store.renew(
                job, worker_id=self.worker_id, lease_s=self.lease_s, run_id=run_id
            )
            is None
        ):
            return
        # Job runs keep their own hidden context; the result reaches the source chat as a
        # card when the job finishes (M74, ADR-0017 §4).
        conversation_id = await self.store.background_conversation(job.user_id)
        with suppress_away_summary():
            await self.runs.start(
                NewRun(
                    id=run_id,
                    user_id=job.user_id,
                    profile="job",
                    input_text=job_prompt(job.goal, job.inputs),
                    conversation_id=conversation_id,
                )
            )
        outcome = await self._supervise(job, run_id)
        if outcome is None:
            return
        record, cancel_reason = outcome
        await self._conclude(job, record, cancel_reason)

    async def _supervise(self, job: Job, run_id: uuid.UUID) -> tuple[RunRecord, str | None] | None:
        """Wait for the run; ``None`` when the lease was lost (the run is cancelled)."""
        started = time.monotonic()
        budget_s = float(job.budget.get("max_duration_seconds") or self.lease_s)
        interval = max(0.05, self.lease_s / 3)
        cancel_reason: str | None = None
        status = "running"
        while True:
            if self.runs.is_live(run_id):
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self.runs.wait_finished(run_id)), timeout=interval
                    )
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)
            record = await self.runs.get(user_id=job.user_id, run_id=run_id)
            if record.terminal:
                return record, cancel_reason
            mirrored = "waiting_approval" if record.status == "waiting_approval" else "running"
            lease = await self.store.renew(
                job,
                worker_id=self.worker_id,
                lease_s=self.lease_s,
                status=mirrored if mirrored != status else None,
            )
            status = mirrored
            if lease is None:
                logger.warning("job_lease_lost", extra={"job_id": str(job.id)})
                await self._cancel_run(job, run_id, "superseded")
                return None
            if lease.cancel_requested and cancel_reason is None:
                cancel_reason = "user"
                await self._cancel_run(job, run_id, "user")
            elif cancel_reason is None and time.monotonic() - started > budget_s:
                cancel_reason = "budget_exceeded"
                await self._cancel_run(job, run_id, "budget_exceeded")

    async def _cancel_run(self, job: Job, run_id: uuid.UUID, reason: Any) -> None:
        try:
            await self.runs.cancel(user_id=job.user_id, run_id=run_id, reason=reason)
        except Exception:
            logger.warning("job_run_cancel_failed", extra={"job_id": str(job.id)})

    async def _conclude(self, job: Job, record: RunRecord, cancel_reason: str | None) -> None:
        if record.status == "completed" and record.response is not None:
            await self.store.finish(
                job,
                worker_id=self.worker_id,
                status="succeeded",
                result=record.response,
                inbox=success_draft(job.goal, record.response),
            )
            return
        if record.status == "cancelled":
            if cancel_reason == "budget_exceeded" or record.cancel_reason == "budget_exceeded":
                await self.store.finish(
                    job,
                    worker_id=self.worker_id,
                    status="failed",
                    error=public_error("job_budget_exceeded"),
                    inbox=failure_draft(job.goal),
                )
            else:
                await self.store.finish(job, worker_id=self.worker_id, status="cancelled")
            return
        error = dict(record.error or public_error("run_internal_error", retryable=True))
        retryable = bool(error.get("retryable"))
        if (
            retryable
            and job.attempt < job.max_attempts
            and not await self.store.run_wrote(record.id)
        ):
            delay = self.settings.jobs_retry_base_seconds * 2 ** (job.attempt - 1)
            await self.store.retry(
                job, worker_id=self.worker_id, delay_s=delay, error_code=str(error.get("code"))
            )
            return
        await self.store.finish(
            job,
            worker_id=self.worker_id,
            status="failed",
            error=error,
            inbox=failure_draft(job.goal),
        )
