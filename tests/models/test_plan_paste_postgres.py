"""ChatGPT sign-in on a hosted backend (M62-T1): the user pastes the loopback redirect address.

The fake auth.openai.com (``AuthWorld``) checks the PKCE verifier and issues the ID token; no
listener runs, so the only way back is ``POST .../sign-in/{id}/callback``.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from app.core.config import Settings
from app.core.crypto import MasterKeyring
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.chatgpt_plan.jwks import JwksVerifier
from app.modules.models.chatgpt_plan.signin import (
    MAX_PASTE_FAILURES,
    AttemptNotPendingError,
    CallbackRejectedError,
    SignInAttempt,
    SignInCoordinator,
    parse_callback,
)
from app.modules.models.composition import build_models_stack
from app.modules.models.models import ModelConnection
from app.modules.models.router import build_models_router
from fastapi import FastAPI
from sqlalchemy import select

from tests.models.auth_world import CLIENT_ID, AuthWorld
from tests.models.conftest import SessionFactory
from tests.models.test_api_postgres import BASE, Api, StaticAuthenticator
from tests.models.test_plan_postgres import settled, store_for

MakeUser = Callable[[], Awaitable[uuid.UUID]]


def hosted(factory: SessionFactory, keyring: MasterKeyring, world: AuthWorld) -> SignInCoordinator:
    return SignInCoordinator(
        session_factory=factory,
        store=store_for(factory, keyring, world, lambda: 1_000.0),
        client=world.client,
        verifier=JwksVerifier(world.client, issuer=oauth.ISSUER),
        timeout_seconds=5.0,
        listen=False,
    )


def authorize_query(attempt: SignInAttempt) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(attempt.authorize_url).query).items()}


def landed(query: dict[str, str], **overrides: str) -> str:
    """The address the browser shows after consent (it never loads on the user's device)."""
    params = {"code": "auth-code", "state": query["state"], "client_id": CLIENT_ID} | overrides
    return f"{query['redirect_uri']}?{urlencode(params)}"


async def test_pasted_redirect_completes_the_attempt_with_the_same_verifier(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    coordinator = hosted(session_factory, keyring, world)
    user = await make_user()
    attempt = await coordinator.start(user)
    query = authorize_query(attempt)
    assert query["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert attempt.listening is False and attempt.server is None
    world.nonce = query["nonce"]

    done = await coordinator.submit_callback(attempt, "  " + landed(query) + "\n")
    assert (done.status, done.error_code) == ("completed", None)
    assert world.code_calls == 1 and done.connection_id is not None
    form = world.last_code_form
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"]
    assert form["redirect_uri"] == query["redirect_uri"] and form["code"] == "auth-code"
    with pytest.raises(AttemptNotPendingError):  # the code is consumed once
        await coordinator.submit_callback(attempt, landed(query))
    async with session_factory() as session:
        row = await session.get(ModelConnection, done.connection_id)
        assert row is not None and row.provider == "chatgpt-plan"
    await coordinator.aclose()


async def test_a_wrong_paste_keeps_the_attempt_until_too_many(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    coordinator = hosted(session_factory, keyring, world)
    attempt = await coordinator.start(await make_user())
    query = authorize_query(attempt)
    world.nonce = query["nonce"]

    with pytest.raises(CallbackRejectedError):  # someone else's sign-in (state differs)
        await coordinator.submit_callback(attempt, landed(query, state="attacker-state"))
    with pytest.raises(CallbackRejectedError):  # not the loopback callback at all
        await coordinator.submit_callback(
            attempt, "https://evil.example/auth/callback?code=x&state=" + query["state"]
        )
    assert attempt.status == "pending" and world.code_calls == 0
    done = await coordinator.submit_callback(attempt, landed(query))
    assert done.status == "completed"

    other = await coordinator.start(await make_user())
    for _ in range(MAX_PASTE_FAILURES):
        with pytest.raises(CallbackRejectedError):
            await coordinator.submit_callback(other, "http://127.0.0.1:1455/auth/callback?x=1")
    assert other.task is not None and other.task.done()  # cancelled, nothing left waiting
    assert (other.status, other.error_code) == ("failed", "too_many_attempts")
    await coordinator.aclose()


async def test_denied_consent_pasted_back_fails_without_a_connection(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    coordinator = hosted(session_factory, keyring, world)
    attempt = await coordinator.start(await make_user())
    query = authorize_query(attempt)
    denied = urlencode({"error": "access_denied", "state": query["state"]})
    url = f"{query['redirect_uri']}?{denied}"
    done = await coordinator.submit_callback(attempt, url)
    assert (done.status, done.error_code) == ("failed", "authorization_denied")
    async with session_factory() as session:
        assert (await session.scalars(select(ModelConnection))).all() == []
    await coordinator.aclose()


async def test_hosted_attempt_times_out_without_a_paste(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    coordinator = hosted(session_factory, keyring, world)
    coordinator._timeout = 0.1  # noqa: SLF001 - shorten the five-minute window
    attempt = await coordinator.start(await make_user())
    await settled(attempt)
    assert (attempt.status, attempt.error_code) == ("failed", "timeout")
    with pytest.raises(AttemptNotPendingError):
        await coordinator.submit_callback(attempt, landed(authorize_query(attempt)))
    await coordinator.aclose()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "http://127.0.0.1:1455/other?code=a&state=b",
        "https://127.0.0.1:1455/auth/callback?code=a&state=b",
        "http://127.0.0.1.evil.example/auth/callback?code=a&state=b",
        "http://127.0.0.1:1455/auth/callback?code=a",
        "javascript:alert(1)",
        "x" * 5000,
    ],
)
def test_parse_callback_accepts_only_the_loopback_redirect(raw: str) -> None:
    with pytest.raises(CallbackRejectedError):
        parse_callback(raw)


def test_parse_callback_takes_a_bare_query_or_a_schemeless_address() -> None:
    assert parse_callback("?code=a&state=b") == {"code": "a", "state": "b"}
    assert parse_callback("127.0.0.1:1455/auth/callback?code=a&state=b")["code"] == "a"
    assert parse_callback("http://localhost:1455/auth/callback?error=x&state=b")["error"] == "x"


async def test_callback_endpoint_completes_and_never_echoes_the_code(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    stack = build_models_stack(
        Settings(app_env="test", chatgpt_plan_loopback_listener=False),
        session_factory,
        keyring=keyring,
        transport=world.transport,
    )
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
        started = (await client.call("POST", f"{BASE}/chatgpt-plan/sign-in", user)).json()
        assert started["listening"] is False
        query = {k: v[0] for k, v in parse_qs(urlsplit(started["authorize_url"]).query).items()}
        world.nonce = query["nonce"]
        url = f"{BASE}/chatgpt-plan/sign-in/{started['attempt_id']}/callback"
        secret_code = "one-time-code-SECRET"

        foreign = await client.call("POST", url, stranger, json={"callback_url": "x"})
        assert foreign.status_code == 404
        wrong = await client.call(
            "POST", url, user, json={"callback_url": landed(query, state="nope", code=secret_code)}
        )
        assert wrong.status_code == 422
        assert wrong.json()["error"]["code"] == "sign_in_callback_invalid"
        extra = await client.call("POST", url, user, json={"callback_url": "x", "more": 1})
        assert extra.status_code == 422 and extra.json()["error"]["code"] == "validation_error"

        done = await client.call(
            "POST", url, user, json={"callback_url": landed(query, code=secret_code)}
        )
        assert done.status_code == 200
        body = done.json()
        assert body["status"] == "completed" and body["connection_id"]
        again = await client.call("POST", url, user, json={"callback_url": landed(query)})
        assert again.status_code == 409 and again.json()["error"]["code"] == "sign_in_not_pending"
        for text in client.bodies:
            assert secret_code not in text and "access-1" not in text
    await stack.aclose()
