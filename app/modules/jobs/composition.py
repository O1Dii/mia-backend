"""Wire jobs, schedules, inbox and notifications (M16) into the application.

In ``app/composition.py`` after ``install_runs`` and ``install_tools``::

    jobs = install_jobs(app, settings, runtime, runs=runs, core_tools=tools.core_tools)

This mounts ``/v1/jobs*``, ``/v1/schedules*``, ``/v1/inbox*``, ``/v1/notify`` and
``/v1/devices/push-token``, registers the ``background.*``/``schedule.*``/``notify`` core
tools, wraps the run feed with the «while you were away» note and exposes
``app.state.jobs`` (for the worker) and ``app.state.notifications`` (M41 registers its
Telegram channel there). The worker process (``python -m app.worker``) executes jobs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI

from app.api.v1.devices import build_devices_router
from app.api.v1.inbox import build_inbox_router
from app.api.v1.jobs import build_jobs_router
from app.core.config import Settings
from app.core.runtime import Runtime
from app.modules.auth.composition import request_authenticator
from app.modules.jobs.deadlines import DeadlineScanner
from app.modules.jobs.domain import utc_now
from app.modules.jobs.service import JobService
from app.modules.jobs.settings import JobsSettings
from app.modules.jobs.store import SqlJobStore
from app.modules.jobs.tools import JobTools, register_job_tools
from app.modules.notifications.away import AwaySummaryFeed
from app.modules.notifications.channels import ChannelRegistry, PushChannel, PushTokenStore
from app.modules.notifications.delivery import DeliveryDispatcher
from app.modules.notifications.fcm import FcmClient, ServiceAccountTokenProvider
from app.modules.notifications.inbox import InboxService
from app.modules.runs.service import RunService
from app.modules.tools.core_tools import CoreToolRegistry


@dataclass(frozen=True, slots=True)
class NotificationsStack:
    inbox: InboxService
    channels: ChannelRegistry
    push_tokens: PushTokenStore
    dispatcher: DeliveryDispatcher


@dataclass(frozen=True, slots=True)
class JobsStack:
    settings: JobsSettings
    store: SqlJobStore
    service: JobService
    scanner: DeadlineScanner
    notifications: NotificationsStack
    runs: RunService


def build_fcm_client(settings: JobsSettings) -> FcmClient | None:
    if settings.fcm_project_id is None or settings.fcm_credentials_file is None:
        return None
    return FcmClient(
        project_id=settings.fcm_project_id,
        token_provider=ServiceAccountTokenProvider(
            settings.fcm_credentials_file, timeout_s=settings.fcm_timeout_seconds
        ),
        endpoint=settings.fcm_endpoint,
        timeout_s=settings.fcm_timeout_seconds,
    )


def build_notifications(
    runtime: Runtime,
    settings: JobsSettings,
    *,
    fcm_client: FcmClient | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> NotificationsStack:
    session_factory = runtime.database.session_factory
    tokens = PushTokenStore(session_factory, clock=clock)
    channels = ChannelRegistry()
    channels.register(PushChannel(tokens, fcm_client or build_fcm_client(settings)))
    return NotificationsStack(
        inbox=InboxService(session_factory, clock=clock),
        channels=channels,
        push_tokens=tokens,
        dispatcher=DeliveryDispatcher(
            session_factory,
            channels,
            max_attempts=settings.notify_delivery_max_attempts,
            clock=clock,
        ),
    )


def build_jobs(
    settings: Settings,
    runtime: Runtime,
    *,
    runs: RunService,
    jobs_settings: JobsSettings | None = None,
    fcm_client: FcmClient | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> JobsStack:
    resolved = jobs_settings or JobsSettings()
    notifications = build_notifications(runtime, resolved, fcm_client=fcm_client, clock=clock)
    store = SqlJobStore(
        runtime.database.session_factory, clock=clock, max_attempts=resolved.jobs_max_attempts
    )
    service = JobService(
        store,
        notifications.inbox,
        resolved,
        runs=runs,
        job_max_steps=settings.run_job_max_steps,
        job_timeout_s=settings.run_job_timeout_seconds,
    )
    return JobsStack(
        settings=resolved,
        store=store,
        service=service,
        scanner=DeadlineScanner(runtime.database.session_factory, clock=clock),
        notifications=notifications,
        runs=runs,
    )


def install_jobs(
    app: FastAPI,
    settings: Settings,
    runtime: Runtime,
    *,
    runs: RunService,
    core_tools: CoreToolRegistry,
    jobs_settings: JobsSettings | None = None,
    fcm_client: FcmClient | None = None,
) -> JobsStack:
    stack = build_jobs(
        settings, runtime, runs=runs, jobs_settings=jobs_settings, fcm_client=fcm_client
    )
    register_job_tools(core_tools, JobTools(stack.service, runs))
    feed = runs.engine.feed
    if feed is not None and not isinstance(feed, AwaySummaryFeed):
        runs.engine.feed = AwaySummaryFeed(feed, stack.notifications.inbox)
    authenticator = request_authenticator(app)
    app.include_router(build_jobs_router(service=stack.service, authenticator=authenticator))
    app.include_router(
        build_inbox_router(
            inbox=stack.notifications.inbox, jobs=stack.service, authenticator=authenticator
        )
    )
    app.include_router(
        build_devices_router(tokens=stack.notifications.push_tokens, authenticator=authenticator)
    )
    app.state.jobs = stack
    app.state.notifications = stack.notifications
    return stack
