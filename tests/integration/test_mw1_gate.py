"""MW1 gate: a run on the primary model, without tools, answers over SSE.

The real M11 ``DefaultModelGateway`` resolves ``mia:default`` to the user's
ChatGPT-plan connection and streams a recorded Responses API answer; the M12
run engine turns it into run events served by ``/v1/runs/{id}/events``. The
live variant of the same path needs the owner's interactive sign-in and is
run by ``python -m app.modules.models.smoke --provider gpt-plan``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app.api.v1.runs import build_runs_router
from app.core.config import Settings
from app.core.crypto import MasterKeyring, generate_master_key
from app.modules.conversations.feed import FeedService
from app.modules.models.adapters import AdapterSettings
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.composition import build_models_stack
from app.modules.realtime.sessions import AuthenticatedUser
from app.modules.runs import EngineConfig, RunEngine, RunNotifier, RunService, SqlRunStore
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.fakes import assert_mvp_valid
from tests.models.auth_world import CLIENT_ID
from tests.models.replay import ReplayTransport
from tests.runs.conftest import TEST_LIMITS, create_users

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
TOKEN = "gate-owner-token"


class StaticAuthenticator:
    def __init__(self, user: uuid.UUID) -> None:
        self.user = user

    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None:
        return AuthenticatedUser(self.user) if bearer_token == TOKEN else None


async def _no_sleep(_seconds: float) -> None:
    return None


def parse_sse(body: str) -> list[dict[str, Any]]:
    frames = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if not line.startswith(":")
        )
        if fields:
            data = json.loads(fields["data"])
            assert fields["id"] == data["cursor"] and fields["event"] == data["type"]
            assert_mvp_valid(data, "run-events")
            frames.append(data)
    return frames


@pytest.fixture
async def gate() -> AsyncIterator[tuple[httpx.AsyncClient, ReplayTransport]]:
    assert TEST_DATABASE_URL is not None
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    await asyncio.to_thread(command.upgrade, configuration, "head")
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
        await connection.execute(text("TRUNCATE TABLE model_gateway_settings"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    (user,) = await create_users(factory, 1)

    transport = ReplayTransport("plan_text")
    stack = build_models_stack(
        Settings(app_env="test"),
        factory,
        keyring=MasterKeyring.from_config(f"k1:{generate_master_key()}"),
        transport=transport,
        adapter_settings=AdapterSettings(pause_seconds=3600, sleep=_no_sleep),
    )
    tokens = oauth.TokenSet(CLIENT_ID, "access-1", "refresh-1", time.time() + 36_000, [])
    await stack.plan_store.save_sign_in(user, tokens, model="gpt-6.1-sol")

    service = RunService(
        RunEngine(
            store=SqlRunStore(factory),
            notifier=RunNotifier(),
            gateway=stack.gateway,
            config=EngineConfig(limits=TEST_LIMITS),
            feed=FeedService(factory),
        ),
        keepalive_s=0.2,
    )
    app = FastAPI()
    app.include_router(build_runs_router(service=service, authenticator=StaticAuthenticator(user)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, transport
    await service.aclose()
    await stack.aclose()
    await engine.dispose()


async def test_primary_model_run_without_tools_answers_over_sse(
    gate: tuple[httpx.AsyncClient, ReplayTransport],
) -> None:
    client, transport = gate
    auth = {"Authorization": f"Bearer {TOKEN}"}

    created = await client.post(
        "/v1/runs",
        json={"profile": "chat", "input": {"text": "Привет"}},
        headers={**auth, "Idempotency-Key": f"gate-{uuid.uuid4()}"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    async with asyncio.timeout(10):
        streamed = await client.get(f"/v1/runs/{run_id}/events", headers=auth)
    assert streamed.status_code == 200
    types = [frame["type"] for frame in parse_sse(streamed.text)]
    assert types[0] == "run.started" and types[-1] == "run.completed"
    assert "response.final" in types and "run.failed" not in types

    run = (await client.get(f"/v1/runs/{run_id}", headers=auth)).json()
    assert run["status"] == "completed"
    assert run["model"].startswith("chatgpt-plan/")
    assert run["response"]["speech"]

    assert transport.remaining == 0
    (request,) = transport.requests
    assert str(request.url) == "https://api.openai.com/v1/responses"
    body = transport.bodies[0]
    assert body["store"] is False and body["stream"] is True
    assert not body.get("tools")
