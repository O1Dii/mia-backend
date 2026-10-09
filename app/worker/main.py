"""The jobs worker process: ``python -m app.worker``.

Loops (each survives transient errors and logs them without content):

- jobs: claim queued jobs up to ``JOBS_WORKER_CONCURRENCY`` and execute them as
  ``profile=job`` runs under a renewed lease;
- scheduler: turn due schedule occurrences into jobs (misfire policy) and reclaim jobs
  whose worker vanished (expired lease);
- deadlines: fire due timer/alarm/focus items of the M14 plugins;
- deliveries: send pending push/telegram deliveries with retries.

All state is in PostgreSQL, so any number of workers may run and a restarted worker
continues where the previous one stopped. SIGTERM stops claiming, waits up to
``JOBS_SHUTDOWN_GRACE_SECONDS`` and hands unfinished jobs back.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable

from app.modules.jobs.composition import JobsStack
from app.modules.jobs.domain import Job
from app.modules.jobs.runner import JobRunner

logger = logging.getLogger(__name__)


def default_worker_id() -> str:
    return f"{socket.gethostname()[:64]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Worker:
    def __init__(self, stack: JobsStack, *, worker_id: str | None = None) -> None:
        self.stack = stack
        self.settings = stack.settings
        self.worker_id = worker_id or default_worker_id()
        self.runner = JobRunner(stack.store, stack.runs, stack.settings, worker_id=self.worker_id)
        self._active: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()
        self._deliveries_wake = asyncio.Event()
        stack.notifications.inbox.on_created = self._deliveries_wake.set

    @property
    def active_jobs(self) -> int:
        return len(self._active)

    # -- one step of each loop (tests drive these directly) ------------------------------

    async def tick_jobs(self) -> int:
        free = self.settings.jobs_worker_concurrency - len(self._active)
        if free <= 0:
            return 0
        jobs = await self.stack.store.claim(
            worker_id=self.worker_id, limit=free, lease_s=self.settings.jobs_lease_seconds
        )
        for job in jobs:
            task = asyncio.create_task(self._execute(job), name=f"job:{job.id}")
            self._active.add(task)
            task.add_done_callback(self._job_done)
        return len(jobs)

    async def _execute(self, job: Job) -> None:
        try:
            await self.runner.execute(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("job_execution_failed", extra={"job_id": str(job.id)})
        finally:
            self._deliveries_wake.set()

    def _job_done(self, task: asyncio.Task[None]) -> None:
        self._active.discard(task)
        self._wake.set()

    async def tick_scheduler(self) -> int:
        fired = await self.stack.store.fire_due(
            grace_s=self.settings.jobs_misfire_grace_seconds,
            budget_for=self.stack.service.default_budget,
        )
        reclaimed = await self.stack.store.reclaim_expired()
        if any(not item.requeued for item in reclaimed):
            self._deliveries_wake.set()
        created = sum(len(item.jobs) for item in fired)
        if created or any(item.requeued for item in reclaimed):
            self._wake.set()
        return created

    async def tick_deadlines(self) -> int:
        fired = await self.stack.scanner.run_once()
        if fired:
            self._deliveries_wake.set()
        return len(fired)

    async def tick_deliveries(self) -> int:
        return await self.stack.notifications.dispatcher.run_once()

    async def wait_idle(self) -> None:
        """Wait for every job this worker is executing (tests)."""
        while self._active:
            await asyncio.gather(*list(self._active), return_exceptions=True)

    # -- process loop ---------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        poll = self.settings.jobs_poll_seconds
        loops = [
            self._loop(stop, self.tick_jobs, poll, self._wake),
            self._loop(stop, self.tick_scheduler, poll, None),
            self._loop(stop, self.tick_deadlines, self.settings.jobs_deadline_scan_seconds, None),
            self._loop(stop, self.tick_deliveries, poll, self._deliveries_wake),
        ]
        logger.info("worker_started", extra={"worker_id": self.worker_id})
        try:
            await asyncio.gather(*loops)
        finally:
            await self.drain(self.settings.jobs_shutdown_grace_seconds)
            logger.info("worker_stopped", extra={"worker_id": self.worker_id})

    async def drain(self, grace_s: float) -> None:
        """Stop: give running jobs ``grace_s`` to finish, then hand the rest back."""
        if not self._active:
            return
        _, pending = await asyncio.wait(set(self._active), timeout=grace_s)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    async def _loop(
        stop: asyncio.Event,
        tick: Callable[[], Awaitable[int]],
        interval: float,
        wake: asyncio.Event | None,
    ) -> None:
        while not stop.is_set():
            try:
                busy = await tick()
            except Exception:
                logger.exception("worker_tick_failed", extra={"tick": tick.__name__})
                busy = 0
            if busy:
                continue
            waiters: list[asyncio.Future[object]] = [asyncio.ensure_future(stop.wait())]
            if wake is not None:
                wake.clear()
                waiters.append(asyncio.ensure_future(wake.wait()))
            _, pending = await asyncio.wait(
                waiters, timeout=interval, return_when=asyncio.FIRST_COMPLETED
            )
            for waiter in pending:
                waiter.cancel()


async def serve() -> None:
    from app.core.config import get_settings
    from app.core.logging import configure_logging
    from app.main import create_app

    settings = get_settings()
    configure_logging(settings.log_level)
    app = create_app(settings)
    stack: JobsStack = app.state.jobs
    worker = Worker(stack)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        await worker.run(stop)
    finally:
        await stack.runs.aclose()
        await app.state.runtime.close()


def main() -> None:
    asyncio.run(serve())
