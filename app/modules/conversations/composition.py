"""Wire chats (M74, ADR-0017): ``/v1/chats*``, ``/v1/feed``, ``/v1/now``, titles, retention.

In ``app/composition.py`` after ``install_jobs`` (so titling wraps the away-summary feed)
and before the Telegram plugin (the bot finds ``app.state.chats``)::

    chats = install_chats(app, runtime, runs=runs, gateway=models_stack.gateway)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI

from app.api.v1.conversations import build_chats_router
from app.api.v1.feed import build_feed_router
from app.api.v1.now import build_now_router
from app.core.ports.model_gateway import ModelGateway
from app.core.runtime import Runtime
from app.modules.auth.composition import request_authenticator
from app.modules.conversations.chats import ChatRetention, ChatRetentionSweeper, ChatService
from app.modules.conversations.feed import FeedService
from app.modules.conversations.titling import ModelChatTitler, TitlingFeed
from app.modules.jobs.now import NowService
from app.modules.runs.service import RunService


@dataclass(frozen=True, slots=True)
class ChatsConfig:
    retention_sweep_s: float = 3600.0
    """Interval of the chat retention sweep in the API process; 0 disables it."""


@dataclass(slots=True)
class ChatsStack:
    chats: ChatService
    feed: FeedService
    now: NowService
    retention: ChatRetention
    sweeper: ChatRetentionSweeper
    titling: TitlingFeed | None = field(default=None)


def install_chats(
    app: FastAPI,
    runtime: Runtime,
    *,
    runs: RunService,
    gateway: ModelGateway | None = None,
    config: ChatsConfig | None = None,
) -> ChatsStack:
    """Mount the routers, wrap the run feed with titling; exposed as ``app.state.chats``."""
    resolved = config or ChatsConfig()
    sessions = runtime.database.session_factory
    retention = ChatRetention(sessions)
    stack = ChatsStack(
        chats=ChatService(sessions),
        feed=FeedService(sessions),
        now=NowService(sessions),
        retention=retention,
        sweeper=ChatRetentionSweeper(retention, resolved.retention_sweep_s),
    )
    feed = runs.engine.feed
    if gateway is not None and feed is not None and not isinstance(feed, TitlingFeed):
        stack.titling = TitlingFeed(feed, session_factory=sessions, titler=ModelChatTitler(gateway))
        runs.engine.feed = stack.titling
    authenticator = request_authenticator(app)
    app.include_router(build_chats_router(service=stack.chats, authenticator=authenticator))
    app.include_router(build_feed_router(service=stack.feed, authenticator=authenticator))
    app.include_router(build_now_router(service=stack.now, authenticator=authenticator))
    app.state.chats = stack

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with original(application):
            stack.sweeper.start()
            try:
                yield
            finally:
                await stack.sweeper.stop()
                if stack.titling is not None:
                    await stack.titling.aclose()

    app.router.lifespan_context = lifespan
    return stack
