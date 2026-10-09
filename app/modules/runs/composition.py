"""Wire the run engine and ``/v1/runs`` into the application.

Ports default to safe placeholders: without a real ``ModelGateway`` (M11) runs
fail with ``model_not_configured``; without a ``ToolRouter`` (M13) runs have no
tools; without ``ApprovalService`` an approval-required call is denied; without
``MemoryProvider`` (M17) no facts are injected.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.v1.runs import RunsAuthenticator, build_runs_router
from app.core.config import Settings
from app.core.ports.approval_service import ApprovalService
from app.core.ports.memory_provider import MemoryProvider
from app.core.ports.model_gateway import ModelGateway
from app.core.ports.tool_router import ToolRouter
from app.core.runtime import Runtime
from app.modules.auth.principal import RequestAuthenticator
from app.modules.conversations.feed import FeedService
from app.modules.runs.defaults import NoToolRouter, UnconfiguredModelGateway
from app.modules.runs.engine import EngineConfig, RunEngine
from app.modules.runs.notifier import RunNotifier
from app.modules.runs.profiles import RunLimits
from app.modules.runs.service import RunService
from app.modules.runs.store import SqlRunStore
from app.modules.runs.summarizer import ModelFeedSummarizer


def build_run_service(
    settings: Settings,
    runtime: Runtime,
    *,
    model_gateway: ModelGateway | None = None,
    tool_router: ToolRouter | None = None,
    approvals: ApprovalService | None = None,
    memory: MemoryProvider | None = None,
) -> RunService:
    gateway = model_gateway or UnconfiguredModelGateway()
    session_factory = runtime.database.session_factory
    engine = RunEngine(
        store=SqlRunStore(session_factory),
        notifier=RunNotifier(),
        gateway=gateway,
        config=EngineConfig(
            limits=RunLimits.from_settings(settings), ack_text=settings.run_speech_ack_text
        ),
        router=tool_router or NoToolRouter(),
        approvals=approvals,
        memory=memory,
        feed=FeedService(
            session_factory,
            recent_entries=settings.run_context_recent_entries,
            summary_batch=settings.run_feed_summary_batch,
        ),
        summarizer_factory=lambda user_id: ModelFeedSummarizer(gateway, user_id),
    )
    return RunService(engine)


def install_runs(
    app: FastAPI,
    settings: Settings,
    runtime: Runtime,
    *,
    authenticator: RunsAuthenticator,
    model_gateway: ModelGateway | None = None,
    tool_router: ToolRouter | None = None,
    approvals: ApprovalService | None = None,
    memory: MemoryProvider | None = None,
) -> RunService:
    """Mount ``/v1/runs*``; the service is also exposed as ``app.state.runs``."""
    service = build_run_service(
        settings,
        runtime,
        model_gateway=model_gateway,
        tool_router=tool_router,
        approvals=approvals,
        memory=memory,
    )
    app.state.runs = service
    web = getattr(app.state, "request_authenticator", None)
    app.include_router(
        build_runs_router(
            service=service,
            authenticator=authenticator,
            web_authenticator=web if isinstance(web, RequestAuthenticator) else None,
        )
    )
    return service
