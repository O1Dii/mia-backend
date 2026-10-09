"""``/v1/runs*`` over HTTP with SSE (PostgreSQL store, fake gateway)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from app.api.v1.runs import build_runs_router
from app.modules.conversations.feed import FeedService
from app.modules.realtime.sessions import AuthenticatedUser
from app.modules.runs import (
    EngineConfig,
    RunEngine,
    RunNotifier,
    RunService,
    SqlRunStore,
)
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes import FakeModelGateway, assert_mvp_valid, text_step
from tests.runs.conftest import TEST_LIMITS, create_users, requires_postgres
from tests.runs.gateways import BlockingGateway, answer

pytestmark = requires_postgres

OWNER_TOKEN = "owner-token"
STRANGER_TOKEN = "stranger-token"


class StaticAuthenticator:
    def __init__(self, users: dict[str, uuid.UUID]) -> None:
        self.users = users

    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None:
        user_id = self.users.get(bearer_token)
        return AuthenticatedUser(user_id) if user_id else None


class Api:
    def __init__(self, client: httpx.AsyncClient, service: RunService) -> None:
        self.client = client
        self.service = service

    async def create(
        self, body: dict[str, Any], *, key: str | None = None, token: str = OWNER_TOKEN
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"}
        headers["Idempotency-Key"] = key or f"idem-{uuid.uuid4()}"
        return await self.client.post("/v1/runs", json=body, headers=headers)

    async def events(
        self, run_id: str, *, token: str = OWNER_TOKEN, **options: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}", **options.pop("headers", {})}
        async with asyncio.timeout(10):
            return await self.client.get(
                f"/v1/runs/{run_id}/events", headers=headers, params=options
            )


def parse_sse(body: str) -> list[dict[str, Any]]:
    frames = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if not line.startswith(":")
        )
        if not fields:
            continue
        data = json.loads(fields["data"])
        assert fields["id"] == data["cursor"] and fields["event"] == data["type"]
        assert_mvp_valid(data, "run-events")
        frames.append(data)
    return frames


@pytest.fixture
async def gateway() -> FakeModelGateway:
    return FakeModelGateway()


@pytest.fixture
async def api(
    pg_factory: async_sessionmaker[AsyncSession], gateway: FakeModelGateway
) -> AsyncIterator[Api]:
    owner, stranger = await create_users(pg_factory, 2)
    engine = RunEngine(
        store=SqlRunStore(pg_factory),
        notifier=RunNotifier(),
        gateway=gateway,
        config=EngineConfig(limits=TEST_LIMITS),
        feed=FeedService(pg_factory),
    )
    service = RunService(engine, keepalive_s=0.2)
    app = FastAPI()
    app.include_router(
        build_runs_router(
            service=service,
            authenticator=StaticAuthenticator({OWNER_TOKEN: owner, STRANGER_TOKEN: stranger}),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Api(client, service)
    await service.aclose()


async def test_create_stream_and_read_run(api: Api, gateway: FakeModelGateway) -> None:
    gateway.add_step(text_step(answer("Привет!")))

    created = await api.create({"profile": "voice", "input": {"text": "Привет"}})
    assert created.status_code == 202
    run = created.json()
    assert run["status"] == "running" and run["profile"] == "voice"
    assert run["events_url"] == f"/v1/runs/{run['id']}/events"

    streamed = await api.events(run["id"])
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(streamed.text)
    assert [frame["type"] for frame in frames] == ["run.started", "response.final", "run.completed"]

    fetched = await api.client.get(
        f"/v1/runs/{run['id']}", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    )
    body = fetched.json()
    assert body["status"] == "completed"
    assert body["response"] == {"speech": "Привет!", "display": None, "followups": []}
    assert body["model"] == "fake/scripted-1" and body["steps"] == 1
    assert "conversation_id" in body and "finished_at" in body


async def test_resume_with_last_event_id_and_cursor(api: Api, gateway: FakeModelGateway) -> None:
    gateway.add_step(text_step(answer("Ок.")))
    run = (await api.create({"profile": "chat", "input": {"text": "x"}})).json()
    frames = parse_sse((await api.events(run["id"])).text)

    by_cursor = parse_sse((await api.events(run["id"], cursor=frames[0]["cursor"])).text)
    assert by_cursor == frames[1:]
    by_header = parse_sse(
        (
            await api.events(
                run["id"],
                cursor=frames[0]["cursor"],
                headers={"Last-Event-ID": frames[1]["cursor"]},
            )
        ).text
    )
    assert by_header == frames[2:]

    other = (await api.create({"profile": "chat", "input": {"text": "y"}})).json()
    gateway.add_step(text_step(answer("Ок.")))
    wrong = await api.events(other["id"], cursor=frames[0]["cursor"])
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "invalid_cursor"


async def test_idempotent_create(api: Api, gateway: FakeModelGateway) -> None:
    gateway.add_step(text_step(answer("Ок.")))
    body = {"profile": "chat", "input": {"text": "один раз"}}
    first = await api.create(body, key="same-key-0000000001")
    second = await api.create(body, key="same-key-0000000001")
    conflict = await api.create(
        {"profile": "chat", "input": {"text": "другое"}}, key="same-key-0000000001"
    )

    assert first.json()["id"] == second.json()["id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    parse_sse((await api.events(first.json()["id"])).text)
    assert len(gateway.requests) == 1


async def test_errors_and_ownership(api: Api, gateway: FakeModelGateway) -> None:
    unauthorized = await api.client.post("/v1/runs", json={})
    assert unauthorized.status_code == 422  # missing Idempotency-Key, body never echoed
    assert unauthorized.json()["error"]["code"] == "validation_error"
    no_token = await api.client.post(
        "/v1/runs",
        json={"profile": "chat", "input": {"text": "x"}},
        headers={"Idempotency-Key": "k" * 20},
    )
    assert no_token.status_code == 401
    invalid = await api.create({"profile": "job", "input": {"text": "x"}})
    assert invalid.status_code == 422 and "job" not in invalid.text
    voice_topic = await api.create(
        {"profile": "voice", "input": {"text": "x"}, "conversation_id": str(uuid.uuid4())}
    )
    assert voice_topic.json()["error"]["code"] == "voice_feed_only"
    missing_topic = await api.create(
        {"profile": "chat", "input": {"text": "x"}, "conversation_id": str(uuid.uuid4())}
    )
    assert missing_topic.status_code == 404

    gateway.add_step(text_step(answer("Ок.")))
    run = (await api.create({"profile": "chat", "input": {"text": "мой"}})).json()
    parse_sse((await api.events(run["id"])).text)
    stranger = {"Authorization": f"Bearer {STRANGER_TOKEN}", "Idempotency-Key": "k" * 20}
    for response in [
        await api.client.get(f"/v1/runs/{run['id']}", headers=stranger),
        await api.events(run["id"], token=STRANGER_TOKEN),
        await api.client.post(f"/v1/runs/{run['id']}/cancel", headers=stranger),
    ]:
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "run_not_found"


async def test_cancel_endpoint(pg_factory: async_sessionmaker[AsyncSession]) -> None:
    (owner,) = await create_users(pg_factory, 1)
    gateway = BlockingGateway(answer("Никогда."))
    service = RunService(
        RunEngine(
            store=SqlRunStore(pg_factory),
            notifier=RunNotifier(),
            gateway=gateway,
            config=EngineConfig(limits=TEST_LIMITS),
        ),
        keepalive_s=0.2,
    )
    app = FastAPI()
    app.include_router(
        build_runs_router(service=service, authenticator=StaticAuthenticator({OWNER_TOKEN: owner}))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        api = Api(client, service)
        run = (await api.create({"profile": "chat", "input": {"text": "долго"}})).json()
        await asyncio.wait_for(gateway.entered.wait(), 2)
        cancelled = await client.post(
            f"/v1/runs/{run['id']}/cancel",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}", "Idempotency-Key": "c" * 20},
        )
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "cancel_requested"
        frames = parse_sse((await api.events(run["id"])).text)
        assert [f["type"] for f in frames] == ["run.started", "run.cancelled"]
