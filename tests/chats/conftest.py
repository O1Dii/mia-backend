"""M74 fixtures: real auth on Postgres, the chats/feed/now/runs routers and a scripted model."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest
from app.api.v1.runs import build_runs_router
from app.composition import AuthServiceAuthenticator
from app.core.ports.model_gateway import ModelGateway
from app.modules.auth.composition import request_authenticator
from app.modules.conversations.chats import ChatService
from app.modules.conversations.composition import ChatsConfig, ChatsStack, install_chats
from app.modules.conversations.feed import FeedService
from app.modules.conversations.models import Conversation, Entry
from app.modules.runs import EngineConfig, NewRun, RunEngine, RunNotifier, RunService, SqlRunStore
from httpx import AsyncClient
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes import FakeModelGateway, text_step
from tests.fakes.contracts import validator_module
from tests.runs.conftest import TEST_LIMITS, collect
from tests.runs.gateways import answer
from tests.workspace.harness import Harness, android_login, build_harness, key

OPENAPI = validator_module().load_mvp_openapi()


def check(schema: str, body: Any) -> None:
    """Validate a response body against ``#/components/schemas/<schema>`` of openapi.yaml."""
    validator = Draft202012Validator(
        {"$ref": f"#/components/schemas/{schema}", "components": OPENAPI["components"]},
        format_checker=FormatChecker(),
    )
    errors = [f"{list(error.path)}: {error.message}" for error in validator.iter_errors(body)]
    assert not errors, errors


class _Database:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = factory


class _Runtime:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.database = _Database(factory)


class EchoGateway(FakeModelGateway):
    """Answers every request with ``Ответ N.``; never runs out of steps."""

    def __init__(self) -> None:
        super().__init__([])
        self.count = 0

    async def stream(self, request: Any) -> Any:
        self.count += 1
        self.add_step(text_step(answer(f"Ответ {self.count}.")))
        async for event in super().stream(request):
            yield event


@dataclass
class ChatWorld:
    harness: Harness
    runs: RunService
    gateway: FakeModelGateway
    stack: ChatsStack

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self.harness.factory

    @property
    def chats(self) -> ChatService:
        return self.stack.chats

    @property
    def feed(self) -> FeedService:
        return self.stack.feed

    async def login(self, client: AsyncClient, subject: str) -> tuple[dict[str, str], uuid.UUID]:
        headers = await android_login(client, subject)
        me = await client.get("/v1/me", headers=headers)
        return headers, uuid.UUID(me.json()["id"])

    async def ask(
        self,
        user_id: uuid.UUID,
        text_value: str,
        *,
        conversation_id: uuid.UUID | None = None,
        profile: str = "chat",
    ) -> list[dict[str, Any]]:
        record, _ = await self.runs.start(
            NewRun(
                user_id=user_id,
                profile=profile,  # type: ignore[arg-type]
                input_text=text_value,
                conversation_id=conversation_id,
            )
        )
        return await collect(self.runs, user_id, record.id)

    async def entries(self, conversation_id: uuid.UUID) -> list[Entry]:
        async with self.factory() as session:
            rows = await session.scalars(
                select(Entry)
                .where(Entry.conversation_id == conversation_id)
                .order_by(Entry.sequence)
            )
            return list(rows)

    async def conversation(self, conversation_id: uuid.UUID) -> Conversation | None:
        async with self.factory() as session:
            return await session.get(Conversation, conversation_id)

    async def set_times(self, conversation_id: uuid.UUID, **values: datetime | None) -> None:
        async with self.factory() as session, session.begin():
            row = await session.get(Conversation, conversation_id)
            assert row is not None
            for name, value in values.items():
                setattr(row, name, value)


async def create_chat(
    client: AsyncClient, headers: dict[str, str], title: str | None = None
) -> dict[str, Any]:
    body = {"title": title} if title else None
    response = await client.post("/v1/chats", json=body, headers={**headers, **key()})
    assert response.status_code == 201, response.text
    check("Chat", response.json())
    return dict(response.json())


@pytest.fixture
async def world() -> AsyncIterator[ChatWorld]:
    async with build_harness() as harness:
        yield await build_world(harness, EchoGateway())


async def build_world(harness: Harness, gateway: ModelGateway) -> ChatWorld:
    engine = RunEngine(
        store=SqlRunStore(harness.factory),
        notifier=RunNotifier(),
        gateway=gateway,
        config=EngineConfig(limits=TEST_LIMITS),
        feed=FeedService(harness.factory),
    )
    runs = RunService(engine, keepalive_s=0.2)
    stack = install_chats(
        harness.app,
        _Runtime(harness.factory),  # type: ignore[arg-type]
        runs=runs,
        config=ChatsConfig(retention_sweep_s=0),
    )
    harness.app.include_router(
        build_runs_router(
            service=runs,
            authenticator=AuthServiceAuthenticator(harness.service),
            web_authenticator=request_authenticator(harness.app),
        )
    )
    assert isinstance(gateway, FakeModelGateway)
    return ChatWorld(harness=harness, runs=runs, gateway=gateway, stack=stack)
