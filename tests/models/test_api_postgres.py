"""``/v1/model-connections``: CRUD never returns a key; keys never reach logs or responses."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from app.core.ports.model_gateway import ModelEvent
from app.modules.models.composition import ModelsStack
from app.modules.models.models import ModelConnection
from app.modules.models.router import build_models_router
from app.modules.models.schemas import CreateConnectionRequest
from app.modules.realtime.sessions import AuthenticatedUser
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select

from tests.models.auth_world import CLIENT_ID, AuthWorld
from tests.models.conftest import SessionFactory
from tests.models.replay import ReplayTransport

MakeUser = Callable[[], Awaitable[uuid.UUID]]
MakeStack = Callable[..., ModelsStack]
SECRET = "sk-test-SUPERSECRETVALUE-0123456789"
SECOND_SECRET = "sk-test-ANOTHERSECRETVAL-9876543210"
BASE = "/v1/model-connections"


class StaticAuthenticator:
    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None:
        if bearer_token.startswith("tok-"):
            return AuthenticatedUser(uuid.UUID(bearer_token[4:]))
        return None


def headers(user: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer tok-{user}"}


class Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.bodies: list[str] = []

    async def call(
        self, method: str, url: str, user: uuid.UUID | None, **kwargs: Any
    ) -> httpx.Response:
        response = await self.client.request(
            method, url, headers=headers(user) if user else {}, **kwargs
        )
        self.bodies.append(response.text)
        return response


@pytest.fixture
async def api(make_stack: MakeStack) -> AsyncIterator[tuple[Api, ModelsStack, ReplayTransport]]:
    transport = ReplayTransport("glm_text", "glm_text")
    stack = make_stack(transport)
    app = FastAPI()
    app.include_router(
        build_models_router(
            authenticator=StaticAuthenticator(),
            connections=stack.connections,
            gateway=stack.gateway,
            plan_store=stack.plan_store,
            sign_in=stack.sign_in,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Api(client), stack, transport
    await stack.aclose()


def create_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider": "deepinfra",
        "label": "GLM fallback",
        "model": "zai-org/GLM-5.3-Flash",
        "api_key": SECRET,
    }
    payload.update(overrides)
    return payload


async def test_crud_never_returns_the_key_only_its_last_four(
    api: tuple[Api, ModelsStack, ReplayTransport],
    make_user: MakeUser,
    session_factory: SessionFactory,
) -> None:
    client, _stack, _ = api
    user = await make_user()
    created = await client.call("POST", BASE, user, json=create_payload())
    assert created.status_code == 201
    body = created.json()
    assert body["has_key"] is True and body["key_last4"] == SECRET[-4:]
    assert "api_key" not in body and "secret_ciphertext" not in body
    assert body["is_primary"] is True and body["verified"] is False and body["status"] == "active"

    listing = (await client.call("GET", BASE, user)).json()["items"]
    assert [c["id"] for c in listing] == [body["id"]]
    one = await client.call("GET", f"{BASE}/{body['id']}", user)
    assert one.json()["key_last4"] == SECRET[-4:]

    patched = await client.call(
        "PATCH", f"{BASE}/{body['id']}", user, json={"label": "Renamed", "api_key": SECOND_SECRET}
    )
    assert (
        patched.json()["label"] == "Renamed" and patched.json()["key_last4"] == SECOND_SECRET[-4:]
    )

    async with session_factory() as session:
        row = await session.scalar(select(ModelConnection))
    assert row is not None and row.secret_ciphertext is not None
    for secret in (SECRET, SECOND_SECRET):
        assert secret.encode() not in row.secret_ciphertext
    assert row.key_last4 == SECOND_SECRET[-4:]

    assert (await client.call("DELETE", f"{BASE}/{body['id']}", user)).status_code == 204
    assert (await client.call("GET", f"{BASE}/{body['id']}", user)).status_code == 404
    for text in client.bodies:
        assert SECRET not in text and SECOND_SECRET not in text


async def test_authentication_and_ownership_are_enforced(
    api: tuple[Api, ModelsStack, ReplayTransport], make_user: MakeUser
) -> None:
    client, _stack, _ = api
    owner, stranger = await make_user(), await make_user()
    assert (await client.call("GET", BASE, None)).status_code == 401
    bad = await client.client.get(BASE, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "unauthenticated"

    connection = (await client.call("POST", BASE, owner, json=create_payload())).json()["id"]
    url = f"{BASE}/{connection}"
    for method, path, kwargs in (
        ("GET", url, {}),
        ("PATCH", url, {"json": {"label": "x"}}),
        ("DELETE", url, {}),
        ("POST", f"{url}/test", {}),
        ("POST", f"{url}/resume", {}),
    ):
        response = await client.call(method, path, stranger, **kwargs)
        assert response.status_code == 404, (method, path)
        assert response.json()["error"]["code"] == "connection_not_found"
    assert (await client.call("GET", BASE, stranger)).json()["items"] == []


@pytest.mark.parametrize(
    ("overrides", "extra"),
    [
        ({"provider": "nope"}, {}),
        ({"provider": "chatgpt-plan"}, {}),
        ({"api_key": None}, {}),  # cloud providers need a key
        ({"base_url": "https://evil.example/v1"}, {}),  # cloud address is fixed
        ({"provider": "local", "base_url": "http://169.254.169.254/v1", "api_key": None}, {}),
        ({"provider": "local", "base_url": "http://127.0.0.1:80/v1", "api_key": None}, {}),
        ({"provider": "anthropic", "capability": "tools"}, {}),  # chat-only in this release
        ({"label": ""}, {}),
        ({}, {"unexpected": 1}),
    ],
)
async def test_invalid_input_is_rejected_without_echoing_the_key(
    overrides: dict[str, Any],
    extra: dict[str, Any],
    api: tuple[Api, ModelsStack, ReplayTransport],
    make_user: MakeUser,
) -> None:
    client, _stack, _ = api
    user = await make_user()
    response = await client.call("POST", BASE, user, json=create_payload(**overrides) | extra)
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {"validation_error", "invalid_connection"}
    assert SECRET not in response.text
    assert (await client.call("GET", BASE, user)).json()["items"] == []


async def test_local_provider_accepts_only_literal_loopback_addresses(
    api: tuple[Api, ModelsStack, ReplayTransport], make_user: MakeUser
) -> None:
    client, _stack, _ = api
    user = await make_user()
    ok = await client.call(
        "POST",
        BASE,
        user,
        json={
            "provider": "local",
            "label": "LM",
            "model": "m",
            "base_url": "http://127.0.0.1:1234/v1",
        },
    )
    assert ok.status_code == 201 and ok.json()["base_url"] == "http://127.0.0.1:1234/v1"
    assert ok.json()["has_key"] is False and ok.json()["capability"] == "chat_only"
    assert ok.json()["is_primary"] is False


async def test_short_keys_reveal_no_characters(
    api: tuple[Api, ModelsStack, ReplayTransport], make_user: MakeUser
) -> None:
    client, _stack, _ = api
    user = await make_user()
    response = await client.call("POST", BASE, user, json=create_payload(api_key="short-key"))
    assert response.json()["has_key"] is True and response.json()["key_last4"] is None


async def test_primary_selection_is_exclusive_and_chat_only_cannot_be_primary(
    api: tuple[Api, ModelsStack, ReplayTransport], make_user: MakeUser
) -> None:
    client, _stack, _ = api
    user = await make_user()
    first = (await client.call("POST", BASE, user, json=create_payload())).json()
    second = (
        await client.call("POST", BASE, user, json=create_payload(provider="groq", model="m"))
    ).json()
    assert first["is_primary"] and not second["is_primary"]
    swapped = await client.call(
        "PATCH", f"{BASE}/{second['id']}", user, json={"make_primary": True}
    )
    assert swapped.json()["is_primary"] is True
    items = (await client.call("GET", BASE, user)).json()["items"]
    assert [i["is_primary"] for i in items] == [False, True]

    chat = (
        await client.call(
            "POST",
            BASE,
            user,
            json=create_payload(provider="anthropic", model="c", api_key=SECOND_SECRET),
        )
    ).json()
    refused = await client.call("PATCH", f"{BASE}/{chat['id']}", user, json={"make_primary": True})
    assert refused.status_code == 422


async def test_test_api_button_runs_one_probe_and_is_rate_limited(
    api: tuple[Api, ModelsStack, ReplayTransport], make_user: MakeUser
) -> None:
    client, _stack, transport = api
    user = await make_user()
    connection = (await client.call("POST", BASE, user, json=create_payload())).json()["id"]
    result = await client.call("POST", f"{BASE}/{connection}/test", user)
    assert result.status_code == 200
    assert result.json()["ok"] is True and result.json()["error_code"] is None
    assert transport.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    stored = (await client.call("GET", f"{BASE}/{connection}", user)).json()
    assert stored["last_test_ok"] is True and stored["last_tested_at"] is not None

    again = await client.call("POST", f"{BASE}/{connection}/test", user)
    assert again.status_code == 429 and again.json()["error"]["retryable"] is True
    assert transport.remaining == 1
    for text in client.bodies:
        assert SECRET not in text


async def test_keys_never_reach_logs_even_when_providers_fail(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    make_stack: MakeStack,
    make_user: MakeUser,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = ReplayTransport("glm_rate_limited", "glm_midstream_error", "plan_503_always")
    stack = make_stack(transport)
    user = await make_user()
    view = await stack.connections.create(
        user, provider="deepinfra", label="GLM", model="m", api_key=SECRET
    )
    probe_failed = await stack.gateway.probe(user, view.id)
    assert probe_failed.ok is False

    from tests.models.helpers import make_request

    request = make_request()
    request = type(request)(
        user_id=str(user), profile="chat", messages=request.messages, model="deepinfra/m"
    )
    seen: list[ModelEvent] = []
    with pytest.raises(Exception) as caught:  # noqa: PT011 - any failure type must stay clean
        async for event in stack.gateway.stream(request):
            seen.append(event)
    assert SECRET not in str(caught.value) and SECRET not in repr(seen)
    await stack.aclose()

    output = capsys.readouterr()
    for blob in (caplog.text, output.out, output.err, repr(view)):
        assert SECRET not in blob
        assert SECRET[-12:] not in blob
    assert all(SECRET not in str(r.__dict__) for r in caplog.records)


def test_request_models_hide_the_key_in_repr_and_dump() -> None:
    payload = CreateConnectionRequest(
        provider="groq",
        label="l",
        model="m",
        api_key=SecretStr(SECRET),
    )
    assert SECRET not in repr(payload) and SECRET not in str(payload)
    assert SECRET not in payload.model_dump_json()


async def test_sign_in_endpoints_start_poll_and_sign_out(
    make_stack: MakeStack, make_user: MakeUser, session_factory: SessionFactory
) -> None:
    world = AuthWorld()
    stack = make_stack(world.transport)
    app = FastAPI()
    app.include_router(
        build_models_router(
            authenticator=StaticAuthenticator(),
            connections=stack.connections,
            gateway=stack.gateway,
            plan_store=stack.plan_store,
            sign_in=stack.sign_in,
        )
    )
    user, stranger = await make_user(), await make_user()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        client = Api(http)
        started = await client.call("POST", f"{BASE}/chatgpt-plan/sign-in", user)
        assert started.status_code == 201
        attempt = started.json()
        assert attempt["authorize_url"].startswith(
            "https://auth.openai.com/api/accounts/authorize?"
        )
        pending = await client.call(
            "GET", f"{BASE}/chatgpt-plan/sign-in/{attempt['attempt_id']}", user
        )
        assert pending.json()["status"] == "pending"
        hidden = await client.call(
            "GET", f"{BASE}/chatgpt-plan/sign-in/{attempt['attempt_id']}", stranger
        )
        assert hidden.status_code == 404

        # finish the flow like the browser would
        from urllib.parse import parse_qs, urlsplit

        query = {k: v[0] for k, v in parse_qs(urlsplit(attempt["authorize_url"]).query).items()}
        world.nonce = query["nonce"]
        async with httpx.AsyncClient() as real:
            await real.get(
                query["redirect_uri"],
                params={"code": "c", "state": query["state"], "client_id": CLIENT_ID},
            )
        for _ in range(100):
            done = (
                await client.call(
                    "GET", f"{BASE}/chatgpt-plan/sign-in/{attempt['attempt_id']}", user
                )
            ).json()
            if done["status"] != "pending":
                break
            import asyncio

            await asyncio.sleep(0.05)
        assert done["status"] == "completed" and done["connection_id"]

        connection = (await client.call("GET", f"{BASE}/{done['connection_id']}", user)).json()
        assert connection["provider"] == "chatgpt-plan" and connection["verified"] is True
        assert connection["has_key"] is True and connection["key_last4"] is None
        for text in client.bodies:
            assert "access-1" not in text and "refresh-1" not in text

        out = await client.call("POST", f"{BASE}/{done['connection_id']}/sign-out", user)
        assert out.json()["status"] == "needs_reauth" and world.revoked == ["refresh-1"]
        assert (
            await client.call("DELETE", f"{BASE}/{done['connection_id']}", user)
        ).status_code == 204
    await stack.aclose()
