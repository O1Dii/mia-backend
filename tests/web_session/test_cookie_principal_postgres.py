"""M30: the web session cookie works on ``/v1/runs*`` and ``/v1/model-connections*``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from app.api.v1.runs import build_runs_router
from app.core.config import Settings
from app.modules.auth.composition import request_authenticator
from app.modules.conversations.feed import FeedService
from app.modules.models.composition import build_models_stack
from app.modules.models.router import build_models_router
from app.modules.runs import EngineConfig, RunEngine, RunNotifier, RunService, SqlRunStore

from tests.fakes import FakeModelGateway, text_step
from tests.runs.conftest import TEST_LIMITS
from tests.runs.gateways import answer
from tests.workspace.harness import (
    BASE_URL,
    ORIGIN,
    Harness,
    build_harness,
    key,
    requires_postgres,
    web_headers,
    web_login,
)

pytestmark = requires_postgres


class NoBearer:
    async def authenticate(self, bearer_token: str) -> None:
        return None


@pytest.fixture
async def world() -> AsyncIterator[tuple[Harness, FakeModelGateway]]:
    async with build_harness() as harness:
        gateway = FakeModelGateway()
        service = RunService(
            RunEngine(
                store=SqlRunStore(harness.factory),
                notifier=RunNotifier(),
                gateway=gateway,
                config=EngineConfig(limits=TEST_LIMITS),
                feed=FeedService(harness.factory),
            ),
            keepalive_s=0.2,
        )
        web = request_authenticator(harness.app)
        harness.app.include_router(
            build_runs_router(service=service, authenticator=NoBearer(), web_authenticator=web)
        )
        models = build_models_stack(Settings(app_env="test"), harness.factory)
        harness.app.include_router(
            build_models_router(
                authenticator=NoBearer(),
                connections=models.connections,
                gateway=models.gateway,
                web_authenticator=web,
            )
        )
        yield harness, gateway
        await service.aclose()
        await models.aclose()


def frames(body: str) -> list[dict[str, object]]:
    blocks = [b for b in body.strip().split("\n\n") if b and not b.startswith(":")]
    return [json.loads(b.split("data: ", 1)[1]) for b in blocks]


async def test_a_signed_in_browser_runs_a_chat_and_streams_its_events(
    world: tuple[Harness, FakeModelGateway],
) -> None:
    harness, gateway = world
    gateway.add_step(text_step(answer("Привет!")))
    async with harness.client() as browser:
        await web_login(browser, "web-runner")
        created = await browser.post(
            "/v1/runs", headers=web_headers(), json={"profile": "chat", "input": {"text": "Привет"}}
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        async with asyncio.timeout(10):
            streamed = await browser.get(f"/v1/runs/{run_id}/events")
        assert [f["type"] for f in frames(streamed.text)][-1] == "run.completed"
        assert (await browser.get(f"/v1/runs/{run_id}")).json()["status"] == "completed"
        assert (
            await browser.post(f"/v1/runs/{run_id}/cancel", headers=web_headers())
        ).status_code == 202


async def test_cookie_requests_are_refused_without_a_session_or_from_another_site(
    world: tuple[Harness, FakeModelGateway],
) -> None:
    harness, _ = world
    body = {"profile": "chat", "input": {"text": "Привет"}}
    async with harness.client() as anonymous:
        refused = await anonymous.post("/v1/runs", headers=key(), json=body)
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] == "authentication_required"
        assert (await anonymous.get("/v1/model-connections")).status_code == 401
    async with harness.client() as browser:
        await web_login(browser, "web-csrf")
        cross = {**key(), "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
        runs = await browser.post("/v1/runs", headers=cross, json=body)
        assert runs.status_code == 403 and runs.json()["error"]["code"] == "permission_denied"
        models = await browser.post(
            "/v1/model-connections",
            headers=cross,
            json={"provider": "local", "label": "x", "model": "m", "base_url": "http://x"},
        )
        assert models.status_code == 403
        # A bearer-looking header never falls back to the cookie.
        bad = await browser.get("/v1/model-connections", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401


async def test_model_connections_work_with_the_cookie_and_stay_per_user(
    world: tuple[Harness, FakeModelGateway],
) -> None:
    harness, _ = world
    async with harness.client() as alice, harness.client() as bob:
        await web_login(alice, "web-alice")
        await web_login(bob, "web-bob")
        created = await alice.post(
            "/v1/model-connections",
            headers={**key(), "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
            json={
                "provider": "deepinfra",
                "label": "GLM",
                "model": "zai-org/GLM-5.3-Flash",
                "api_key": "sk-test-0123456789abcd",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["key_last4"] == "abcd" and "api_key" not in created.json()
        assert [c["id"] for c in (await alice.get("/v1/model-connections")).json()["items"]] == [
            created.json()["id"]
        ]
        assert (await bob.get("/v1/model-connections")).json()["items"] == []
        assert BASE_URL.startswith("http")  # same-origin base used by the harness
        assert isinstance(alice, httpx.AsyncClient)
