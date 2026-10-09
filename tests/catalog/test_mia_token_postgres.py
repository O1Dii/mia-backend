"""M51-R1: ``server.auth: mia_token`` end to end in the store (publish → install → connect → call).

The remote MCP server of the test world runs without its own authorization server here
(``require_auth=False``) and records what Mia sends; each bearer is then checked against
Mia's authorization server, exactly what a real plugin does through ``/oauth/userinfo``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.core.ports.tool_router import ToolCallRequest
from app.modules.connections.oauth.models import PluginConnection
from app.modules.oauth.cimd import ClientMetadataResolver
from app.modules.oauth.idtoken import IdTokenSigner
from app.modules.oauth.models import OAuthGrant, OAuthToken
from app.modules.oauth.service import AuthorizationService, OAuthConfig, utc_now
from app.modules.plugins.catalog.package import load_package
from sqlalchemy import select

from tests.catalog.harness import PUBLIC_BASE, Store, build_store, key
from tests.catalog.remote_world import MCP_HOST, RESOURCE
from tests.catalog.test_connections_postgres import context
from tests.catalog.test_install_postgres import _json, install, tracker, write
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres

APP_CLIENT = f"https://{MCP_HOST}/oauth/client.json"


@pytest.fixture
async def world() -> AsyncIterator[tuple[Store, AuthorizationService]]:
    async with build_store(require_auth=False, camel_case_tool=False) as store:
        service = AuthorizationService(
            sessions=store.factory,
            clients=ClientMetadataResolver(),
            signer=IdTokenSigner(b"m51-r1-test-secret-m51-r1-test-secret"),
            config=OAuthConfig(issuer=PUBLIC_BASE, consent_url=f"{PUBLIC_BASE}/oauth.html"),
            subject_key=hashlib.sha256(b"m51-r1").digest(),
        )
        store.connections.use_mia_tokens(service)
        yield store, service


def manifest(client_id: str | None = APP_CLIENT) -> dict[str, Any]:
    body = tracker()
    body["server"]["auth"] = "mia_token"
    if client_id is not None:
        body["server"]["clientId"] = client_id
    return body


async def publish_and_install(store: Store, tmp_path: Path, user: dict[str, str]) -> str:
    package = load_package(write(tmp_path, manifest()))
    await store.publish(package, token="discovery-token")  # the test server takes any bearer
    installed = _json(await install(store, user, "team.tracker"), 201)
    assert installed["status"] == "needs_connection"
    return str(installed["id"])


async def sign_in_to_the_app(
    store: Store, user_id: str, *, client_id: str = APP_CLIENT, scopes: str = "openid email"
) -> uuid.UUID:
    """What «Войти через Mia» in the plugin's app leaves behind: an active grant."""
    grant = uuid.uuid4()
    async with store.factory() as session, session.begin():
        session.add(
            OAuthGrant(
                id=grant,
                user_id=uuid.UUID(user_id),
                client_id=client_id,
                client_name="Tracker app",
                scopes=scopes,
                monthly_limit_usd=5,
            )
        )
    return grant


async def connect(store: Store, user: dict[str, str], installation_id: str) -> httpx.Response:
    return await store.post(user, "/v1/connections", {"installation_id": installation_id})


def last_bearer(store: Store) -> str:
    header = store.remote.mcp_requests[-1].get("authorization", "")
    assert header.startswith("Bearer "), header
    return header.removeprefix("Bearer ")


async def test_mia_token_plugin_connects_through_the_users_grant_and_gets_minted_tokens(
    world: tuple[Store, AuthorizationService], tmp_path: Path
) -> None:
    store, service = world
    ann = await store.login("ann")
    ann_id = await store.user_id(ann)
    installation_id = await publish_and_install(store, tmp_path, ann)

    # Not signed in to the app with Mia yet: connecting says so, nothing is minted.
    refused = await connect(store, ann, installation_id)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "connection_sign_in_required"
    assert refused.json()["error"]["details"] == {"client_id": APP_CLIENT}

    await sign_in_to_the_app(store, ann_id)
    connected = _json(await connect(store, ann, installation_id), 200)
    assert connected["status"] == "connected"
    view = _json(await store.get(ann, f"/v1/installations/{installation_id}"), 200)
    assert view["status"] == "active"
    listed = _json(await store.get(ann, "/v1/connections"), 200)["items"]
    assert [(c["plugin_id"], c["status"], c["account_label"]) for c in listed] == [
        ("team.tracker", "connected", "Tracker app")
    ]

    ctx = await context(store, ann)
    result = await store.router.call(ctx, ToolCallRequest("c1", "team.tracker.list_issues", {}))
    assert result.status == "succeeded", result
    token = last_bearer(store)
    access = await service.authenticate(token)
    assert access is not None
    assert (access.client_id, access.resource, str(access.user_id)) == (
        APP_CLIENT,
        RESOURCE,
        ann_id,
    )
    assert access.scopes == frozenset({"openid"})  # identity only, even though email is granted
    assert (access.expires_at - utc_now()).total_seconds() <= 300
    async with store.factory() as session:
        kinds = (
            await session.scalars(select(OAuthToken.kind).where(OAuthToken.resource == RESOURCE))
        ).all()
        row = await session.scalar(select(PluginConnection))
    assert set(kinds) == {"access"}  # never a refresh token
    assert row is not None and row.secret_ciphertext is None and row.auth_method == "mia_token"

    # Each call gets a fresh token.
    again = await store.router.call(ctx, ToolCallRequest("c2", "team.tracker.list_issues", {}))
    assert again.status == "succeeded" and last_bearer(store) != token


async def test_revoking_the_app_grant_stops_minting_and_kills_minted_tokens(
    world: tuple[Store, AuthorizationService], tmp_path: Path
) -> None:
    store, service = world
    ann = await store.login("ann")
    ann_id = await store.user_id(ann)
    installation_id = await publish_and_install(store, tmp_path, ann)
    grant = await sign_in_to_the_app(store, ann_id)
    _json(await connect(store, ann, installation_id), 200)
    ctx = await context(store, ann)
    assert (
        await store.router.call(ctx, ToolCallRequest("c1", "team.tracker.list_issues", {}))
    ).status == "succeeded"
    token = last_bearer(store)

    await service.revoke_grant(uuid.UUID(ann_id), grant)

    assert await service.authenticate(token) is None
    sent = len(store.remote.mcp_requests)
    after = await store.router.call(ctx, ToolCallRequest("c2", "team.tracker.list_issues", {}))
    assert (after.status, after.error_code) == ("failed", "connection_required")
    assert len(store.remote.mcp_requests) == sent  # nothing reached the server
    headers = await store.connections.headers(
        user_id=ann_id, installation_id=installation_id, plugin_id="team.tracker", auth="mia_token"
    )
    assert headers is None


async def test_no_token_for_a_user_without_a_grant_of_that_client(
    world: tuple[Store, AuthorizationService], tmp_path: Path
) -> None:
    store, _ = world
    ann = await store.login("ann")
    bob = await store.login("bob")
    ann_id, bob_id = await store.user_id(ann), await store.user_id(bob)
    ann_install = await publish_and_install(store, tmp_path, ann)
    bob_install = _json(await install(store, bob, "team.tracker"), 201)["id"]
    await sign_in_to_the_app(store, ann_id)
    _json(await connect(store, ann, ann_install), 200)
    # Bob signed in to some other app with Mia, never to this plugin's app.
    await sign_in_to_the_app(store, bob_id, client_id="https://notes.example/client.json")
    assert (await connect(store, bob, bob_install)).status_code == 409

    for user_id, installation_id in ((bob_id, bob_install), (bob_id, ann_install)):
        assert (
            await store.connections.headers(
                user_id=user_id,
                installation_id=installation_id,
                plugin_id="team.tracker",
                auth="mia_token",
            )
            is None
        )
    # Ann's grant without openid cannot mint either.
    async with store.factory() as session, session.begin():
        grant = await session.scalar(select(OAuthGrant).where(OAuthGrant.client_id == APP_CLIENT))
        assert grant is not None
        grant.scopes = "email"
    assert (
        await store.connections.headers(
            user_id=ann_id, installation_id=ann_install, plugin_id="team.tracker", auth="mia_token"
        )
        is None
    )


async def test_disconnect_stops_the_tokens_without_touching_the_app_grant(
    world: tuple[Store, AuthorizationService], tmp_path: Path
) -> None:
    store, service = world
    ann = await store.login("ann")
    ann_id = await store.user_id(ann)
    installation_id = await publish_and_install(store, tmp_path, ann)
    await sign_in_to_the_app(store, ann_id)
    connection = _json(await connect(store, ann, installation_id), 200)["connection_id"]
    gone = await store.client.delete(f"/v1/connections/{connection}", headers={**ann, **key()})
    assert gone.status_code == 204, gone.text
    view = _json(await store.get(ann, f"/v1/installations/{installation_id}"), 200)
    assert view["status"] == "needs_connection"
    assert (
        await store.connections.headers(
            user_id=ann_id,
            installation_id=installation_id,
            plugin_id="team.tracker",
            auth="mia_token",
        )
        is None
    )
    assert await service.plugin_grant(uuid.UUID(ann_id), APP_CLIENT) is not None


async def test_publication_requires_a_valid_client_id_for_mia_token(
    world: tuple[Store, AuthorizationService], tmp_path: Path
) -> None:
    store, _ = world
    from app.modules.plugins.catalog.errors import PackageInvalidError

    for index, client_id in enumerate(
        (None, "https://elsewhere.example/client.json", "http://localhost:8000/client.json")
    ):
        body = manifest(client_id)
        body["version"] = f"1.0.{index}"
        with pytest.raises(PackageInvalidError) as refused:
            await store.publish(load_package(write(tmp_path, body)), token="t")
        assert [p["path"] for p in refused.value.problems] == ["server.clientId"], json.dumps(
            refused.value.problems
        )
