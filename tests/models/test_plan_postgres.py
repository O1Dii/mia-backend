"""ChatGPT plan credentials: sealed storage, serialized refresh, interactive sign-in."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app.core.config import Settings
from app.core.crypto import MasterKeyring
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.chatgpt_plan.credentials import PlanCredentialStore
from app.modules.models.chatgpt_plan.jwks import JwksVerifier
from app.modules.models.chatgpt_plan.signin import SignInAttempt, SignInCoordinator
from app.modules.models.composition import ModelsStack, build_models_stack
from app.modules.models.errors import ProviderError
from app.modules.models.models import GatewaySetting, ModelConnection
from app.modules.models.vault import KeyVault
from sqlalchemy import select

from tests.models.auth_world import CLIENT_ID, AuthWorld
from tests.models.conftest import SessionFactory
from tests.models.jwt_tools import RsaSigner

MakeUser = Callable[[], Awaitable[uuid.UUID]]


def store_for(
    factory: SessionFactory, keyring: MasterKeyring, world: AuthWorld, clock: Any
) -> PlanCredentialStore:
    return PlanCredentialStore(factory, KeyVault(keyring), world.client, clock=clock)


async def expired_connection(
    store: PlanCredentialStore, user: uuid.UUID, world: AuthWorld, *, expires_at: float = 100.0
) -> uuid.UUID:
    world.counter = 1
    world.current_refresh = "refresh-1"
    tokens = oauth.TokenSet(CLIENT_ID, "access-1", "refresh-1", expires_at=expires_at, scopes=[])
    return await store.save_sign_in(user, tokens)


async def test_concurrent_requests_trigger_exactly_one_rotation_even_across_workers(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    world.delay = 0.05
    user = await make_user()
    clock = lambda: 10_000.0  # noqa: E731 - token expired at t=100
    worker_a = store_for(session_factory, keyring, world, clock)
    worker_b = store_for(session_factory, keyring, world, clock)  # separate in-process locks
    connection = await expired_connection(worker_a, user, world)

    tokens = await asyncio.gather(
        *(w.access_token(user, connection) for w in (worker_a, worker_b) for _ in range(4))
    )
    assert world.refresh_calls == 1
    assert set(tokens) == {"access-2"}
    # the rotated refresh token was persisted atomically: the next rotation succeeds
    later = lambda: 100_000.0  # noqa: E731
    assert (
        await store_for(session_factory, keyring, world, later).access_token(user, connection)
        == "access-3"
    )
    assert world.refresh_calls == 2


async def test_tokens_are_sealed_at_rest(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    user = await make_user()
    store = store_for(session_factory, keyring, world, lambda: 10_000.0)
    connection = await expired_connection(store, user, world)
    await store.access_token(user, connection)
    async with session_factory() as session:
        row = await session.get(ModelConnection, connection)
    assert row is not None and row.secret_ciphertext is not None
    for needle in (b"access-2", b"refresh-2", b"access-1", CLIENT_ID.encode()):
        assert needle not in row.secret_ciphertext
    assert row.key_last4 is None


async def test_a_rejected_token_forces_a_refresh_even_when_it_looks_fresh(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    user = await make_user()
    store = store_for(session_factory, keyring, world, lambda: 100.0)
    connection = await expired_connection(store, user, world, expires_at=10_000.0)
    source = store.token_source(user, connection)
    assert await source.access_token() == "access-1"
    await source.invalidate()  # the API answered 401 for access-1
    assert await source.access_token() == "access-2"
    assert world.refresh_calls == 1


async def test_dead_refresh_token_clears_tokens_keeps_client_id_and_fails_fast(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    user = await make_user()
    store = store_for(session_factory, keyring, world, lambda: 10_000.0)
    connection = await expired_connection(store, user, world)
    world.refresh_failure = (400, {"error": "invalid_grant"})

    with pytest.raises(ProviderError) as caught:
        await store.access_token(user, connection)
    assert caught.value.needs_reauth and caught.value.code == "model_not_configured"
    async with session_factory() as session:
        row = await session.get(ModelConnection, connection)
        assert row is not None and row.status == "needs_reauth"
    assert await store.saved_client_id(user) == CLIENT_ID  # re-login reuses the issued client

    calls = world.refresh_calls
    with pytest.raises(ProviderError):
        await store.access_token(user, connection)
    assert world.refresh_calls == calls  # no further upstream traffic


async def test_transient_refresh_failure_keeps_the_session(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    user = await make_user()
    store = store_for(session_factory, keyring, world, lambda: 10_000.0)
    connection = await expired_connection(store, user, world)
    world.refresh_failure = (503, {"error": "temporarily_unavailable"})
    with pytest.raises(ProviderError) as caught:
        await store.access_token(user, connection)
    assert caught.value.code == "provider_unavailable" and caught.value.retryable
    world.refresh_failure = None
    assert await store.access_token(user, connection) == "access-2"


async def test_sign_out_revokes_and_requires_a_new_login(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    user = await make_user()
    store = store_for(session_factory, keyring, world, lambda: 100.0)
    connection = await expired_connection(store, user, world, expires_at=10_000.0)
    assert await store.sign_out(user, connection) is True
    assert world.revoked == ["refresh-1"]
    with pytest.raises(ProviderError) as caught:
        await store.access_token(user, connection)
    assert caught.value.needs_reauth


# ------------------------------------------------------------- interactive sign-in


def make_coordinator(
    factory: SessionFactory, keyring: MasterKeyring, world: AuthWorld, *, timeout: float = 5.0
) -> tuple[SignInCoordinator, PlanCredentialStore]:
    store = store_for(factory, keyring, world, lambda: 1_000.0)
    verifier = JwksVerifier(world.client, issuer=oauth.ISSUER)
    coordinator = SignInCoordinator(
        session_factory=factory,
        store=store,
        client=world.client,
        verifier=verifier,
        preferred_port=0,
        timeout_seconds=timeout,
    )
    return coordinator, store


async def browser_returns(attempt: SignInAttempt, **overrides: str) -> dict[str, str]:
    query = {k: v[0] for k, v in parse_qs(urlsplit(attempt.authorize_url).query).items()}
    params = {"code": "auth-code", "state": query["state"], "client_id": CLIENT_ID} | overrides
    async with httpx.AsyncClient() as real:  # a real loopback request, not the fake world
        await real.get(query["redirect_uri"], params=params)
    return query


async def settled(attempt: SignInAttempt) -> SignInAttempt:
    assert attempt.task is not None
    await asyncio.wait_for(asyncio.shield(attempt.task), 5)
    return attempt


async def test_sign_in_end_to_end_verifies_the_id_token_and_stores_sealed_tokens(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    user = await make_user()
    coordinator, store = make_coordinator(session_factory, keyring, world)

    attempt = await coordinator.start(user)
    first = {k: v[0] for k, v in parse_qs(urlsplit(attempt.authorize_url).query).items()}
    assert first["client_id"] == "dynamic_agent_client"
    assert first["ext_agent_host_id"].startswith("urn:uuid:")
    world.nonce = first["nonce"]
    await browser_returns(attempt)
    assert (await settled(attempt)).status == "completed"
    assert attempt.error_code is None and attempt.connection_id is not None
    assert coordinator.status(user, attempt.id) is attempt
    assert coordinator.status(uuid.uuid4(), attempt.id) is None  # not visible to others

    source = store.token_source(user, attempt.connection_id)
    assert await source.access_token() == "access-1"
    async with session_factory() as session:
        row = await session.get(ModelConnection, attempt.connection_id)
        assert row is not None and (row.provider, row.is_primary, row.status) == (
            "chatgpt-plan",
            True,
            "active",
        )
        host = await session.get(GatewaySetting, "ext_agent_host_id")
        assert host is not None and host.value == first["ext_agent_host_id"]

    # second login: same persistent host id, issued client id, no name hint
    again = await coordinator.start(user)
    second = {k: v[0] for k, v in parse_qs(urlsplit(again.authorize_url).query).items()}
    assert second["ext_agent_host_id"] == first["ext_agent_host_id"]
    assert second["client_id"] == CLIENT_ID and "agent_name_hint" not in second
    await coordinator.aclose()


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("bad_state", "state_mismatch"),
        ("denied", "authorization_denied"),
        ("dynamic_client", "client_not_registered"),
        ("forged_id_token", "id_token_invalid"),
        ("no_direct_scope", "scope_missing"),
    ],
)
async def test_sign_in_failures_create_no_connection(
    scenario: str,
    expected: str,
    session_factory: SessionFactory,
    keyring: MasterKeyring,
    make_user: MakeUser,
) -> None:
    world = AuthWorld()
    user = await make_user()
    coordinator, _store = make_coordinator(session_factory, keyring, world)
    attempt = await coordinator.start(user)
    world.nonce = parse_qs(urlsplit(attempt.authorize_url).query)["nonce"][0]
    overrides: dict[str, str] = {}
    if scenario == "bad_state":
        overrides["state"] = "attacker-state"
    elif scenario == "denied":
        overrides = {"error": "access_denied"}
    elif scenario == "dynamic_client":
        overrides["client_id"] = "dynamic_agent_client"
    elif scenario == "forged_id_token":
        world.id_token_signer = RsaSigner("kid-1")  # same kid, attacker's key
    elif scenario == "no_direct_scope":
        world.scope = "openid offline_access"
    await browser_returns(attempt, **overrides)
    await settled(attempt)
    assert (attempt.status, attempt.error_code) == ("failed", expected)
    async with session_factory() as session:
        assert (await session.scalars(select(ModelConnection))).all() == []
    await coordinator.aclose()


async def test_sign_in_times_out_and_frees_the_port(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    coordinator, _ = make_coordinator(session_factory, keyring, world, timeout=0.1)
    attempt = await coordinator.start(await make_user())
    await settled(attempt)
    assert (attempt.status, attempt.error_code) == ("failed", "timeout")
    redirect = parse_qs(urlsplit(attempt.authorize_url).query)["redirect_uri"][0]
    async with httpx.AsyncClient() as real:
        with pytest.raises(httpx.TransportError):
            await real.get(redirect)  # the listener is gone
    await coordinator.aclose()


async def test_starting_again_supersedes_the_pending_attempt(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    world = AuthWorld()
    user = await make_user()
    coordinator, _ = make_coordinator(session_factory, keyring, world)
    first = await coordinator.start(user)
    second = await coordinator.start(user)
    assert first.task is not None
    await asyncio.wait_for(asyncio.gather(first.task, return_exceptions=True), 5)
    assert first.error_code == "superseded" and second.status == "pending"
    await coordinator.aclose()


def test_stack_wires_the_plan_pieces(
    session_factory: SessionFactory, keyring: MasterKeyring
) -> None:
    stack: ModelsStack = build_models_stack(
        Settings(app_env="test"), session_factory, keyring=keyring
    )
    assert stack.plan_store is not None and stack.sign_in is not None
