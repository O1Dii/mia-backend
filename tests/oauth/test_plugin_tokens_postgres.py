"""M51-R1: plugin servers as RFC 8707 resources, ``userinfo`` audience, minted ``mia_token``s.

Security properties covered here:

- a resource Mia does not know is ``invalid_target``; a published plugin server is accepted
  only for its own client (``server.clientId``) or Mia's connection client; an add-by-URL
  server only for Mia's connection client; ``OAUTH_EXTRA_RESOURCES`` for any client;
- audience confusion: a token bound to a plugin server is refused by Mia's own API and
  ``/mcp`` but answers on ``/oauth/userinfo`` with ``aud`` and ``azp``;
- minted tokens: only from the user's live grant of that exact client, scopes never exceed
  the grant, at most 5 minutes, no refresh token, dead as soon as the grant is revoked.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app.modules.connections.resources import PluginResources
from app.modules.oauth.models import OAuthGrant, OAuthToken
from app.modules.oauth.service import OAuthConfig, utc_now
from app.modules.plugins.catalog.models import PluginRelease
from app.modules.workspace.models import Installation
from app.modules.workspace.service import ensure_personal_workspace
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.oauth.harness import (
    CLIENT_ID,
    CONSENT,
    ISSUER,
    Documents,
    OAuthWorld,
    client_document,
    oauth_world,
    pkce_pair,
)
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres

PLUGIN = "https://maps.example/mcp"
PLUGIN_CLIENT = "https://maps.example/oauth/client.json"
PLUGIN_REDIRECT = "https://maps.example/cb"
OTHER = "https://other.example/mcp"
CUSTOM = "https://custom.example/mcp"
EXTRA = "http://localhost:8000/mcp"
MIA_CLIENT = f"{ISSUER}/v1/connections/client-metadata.json"
MIA_REDIRECT = f"{ISSUER}/v1/connections/callback"


def documents() -> Documents:
    return Documents(
        {
            CLIENT_ID: client_document(),
            PLUGIN_CLIENT: client_document(
                client_id=PLUGIN_CLIENT, client_name="Maps", redirect_uris=[PLUGIN_REDIRECT]
            ),
            MIA_CLIENT: client_document(
                client_id=MIA_CLIENT, client_name="Mia", redirect_uris=[MIA_REDIRECT]
            ),
        }
    )


@pytest.fixture
async def world() -> Any:
    config = OAuthConfig(issuer=ISSUER, consent_url=CONSENT, extra_resources=frozenset({EXTRA}))
    async with oauth_world(config=config, documents=documents()) as built:
        built.stack.service.resources = PluginResources(
            built.factory, connection_client_id=MIA_CLIENT
        )
        async with built.factory() as session, session.begin():
            await session.execute(delete(PluginRelease))
            session.add(release("team.maps", PLUGIN, client_id=PLUGIN_CLIENT))
            session.add(release("team.other", OTHER, client_id=None, auth="oauth"))
        await add_custom_install(built.factory, built.users[1], CUSTOM)
        yield built


def release(
    plugin_id: str, url: str, *, client_id: str | None, auth: str = "mia_token"
) -> PluginRelease:
    server: dict[str, Any] = {"kind": "remote", "url": url, "auth": auth}
    if client_id is not None:
        server["clientId"] = client_id
    return PluginRelease(
        plugin_id=plugin_id,
        version="1.0.0",
        version_key="1.0.0",
        name=plugin_id,
        description="test",
        category="knowledge",
        server_kind="remote",
        manifest={"id": plugin_id, "server": server},
        tools=[],
        tools_digest="sha256:test",
        grants=[],
        package_digest="sha256:pkg",
    )


async def add_custom_install(
    factory: async_sessionmaker[AsyncSession], user: uuid.UUID, url: str
) -> None:
    async with factory() as session, session.begin():
        workspace = await ensure_personal_workspace(session, user)
        session.add(
            Installation(
                workspace_id=workspace,
                plugin_id="custom.u" + uuid.uuid4().hex[:15],
                version="0.0.0",
                status="needs_consent",
                source="custom",
                custom_url=url,
            )
        )


def query(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def start(
    world: OAuthWorld, http: httpx.AsyncClient, *, client_id: str, redirect: str, resource: str
) -> httpx.Response:
    return await world.authorize(
        http,
        scope="openid email",
        challenge=pkce_pair()[1],
        client_id=client_id,
        redirect_uri=redirect,
        extra={"resource": resource},
    )


def target_error(response: httpx.Response) -> str | None:
    assert response.status_code == 302, response.text
    return query(response.headers["location"]).get("error")


async def token_for(
    world: OAuthWorld, http: httpx.AsyncClient, *, client_id: str, redirect: str, resource: str
) -> dict[str, Any]:
    verifier, challenge = pkce_pair()
    started = await world.authorize(
        http,
        scope="openid email llm.generate",
        challenge=challenge,
        client_id=client_id,
        redirect_uri=redirect,
        extra={"resource": resource},
    )
    decided = await world.consent(http, started.headers["location"])
    code = query(decided.json()["redirect_to"])["code"]
    response = await http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect,
        },
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def mcp_list(http: httpx.AsyncClient, token: str) -> httpx.Response:
    return await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            **bearer(token),
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
        },
    )


# -- R1: which resources the authorization server accepts -----------------------------------------


async def test_an_unknown_resource_is_invalid_target(world: OAuthWorld) -> None:
    async with world.http() as http:
        for resource in ("https://unknown.example/mcp", "https://maps.example/other", "not a url"):
            response = await start(
                world, http, client_id=PLUGIN_CLIENT, redirect=PLUGIN_REDIRECT, resource=resource
            )
            assert target_error(response) == "invalid_target", resource


async def test_a_published_plugin_server_is_a_resource_for_its_own_client_only(
    world: OAuthWorld,
) -> None:
    async with world.http() as http:
        own = await start(
            world, http, client_id=PLUGIN_CLIENT, redirect=PLUGIN_REDIRECT, resource=PLUGIN
        )
        # canonical form: case of the host and a trailing slash do not matter
        canonical = await start(
            world,
            http,
            client_id=PLUGIN_CLIENT,
            redirect=PLUGIN_REDIRECT,
            resource="https://MAPS.example/mcp/",
        )
        via_mia = await start(
            world, http, client_id=MIA_CLIENT, redirect=MIA_REDIRECT, resource=PLUGIN
        )
        stranger = await start(
            world,
            http,
            client_id=CLIENT_ID,
            redirect=client_document()["redirect_uris"][0],
            resource=PLUGIN,
        )
    assert target_error(own) is None and own.headers["location"].startswith(CONSENT)
    assert target_error(canonical) is None
    assert target_error(via_mia) is None
    assert target_error(stranger) == "invalid_target"


async def test_servers_without_client_id_and_added_by_url_are_for_mias_connection_client_only(
    world: OAuthWorld,
) -> None:
    redirect = client_document()["redirect_uris"][0]
    async with world.http() as http:
        released = await start(
            world, http, client_id=MIA_CLIENT, redirect=MIA_REDIRECT, resource=OTHER
        )
        custom = await start(
            world, http, client_id=MIA_CLIENT, redirect=MIA_REDIRECT, resource=CUSTOM
        )
        app_released = await start(
            world, http, client_id=CLIENT_ID, redirect=redirect, resource=OTHER
        )
        app_custom = await start(
            world, http, client_id=CLIENT_ID, redirect=redirect, resource=CUSTOM
        )
    assert target_error(released) is None and target_error(custom) is None
    assert target_error(app_released) == "invalid_target"
    assert target_error(app_custom) == "invalid_target"


async def test_oauth_extra_resources_is_an_override_for_any_client(world: OAuthWorld) -> None:
    redirect = client_document()["redirect_uris"][0]
    async with world.http() as http:
        response = await start(world, http, client_id=CLIENT_ID, redirect=redirect, resource=EXTRA)
    assert target_error(response) is None


async def test_without_a_policy_only_own_and_extra_resources_are_accepted(
    world: OAuthWorld,
) -> None:
    world.stack.service.resources = None
    async with world.http() as http:
        plugin = await start(
            world, http, client_id=PLUGIN_CLIENT, redirect=PLUGIN_REDIRECT, resource=PLUGIN
        )
        own = await start(
            world, http, client_id=PLUGIN_CLIENT, redirect=PLUGIN_REDIRECT, resource=f"{ISSUER}/mcp"
        )
    assert target_error(plugin) == "invalid_target"
    assert target_error(own) is None


# -- R2: userinfo audience and audience confusion ---------------------------------------------


async def test_userinfo_reports_the_audience_and_mias_apis_refuse_a_plugin_token(
    world: OAuthWorld,
) -> None:
    async with world.http() as http:
        tokens = await token_for(
            world, http, client_id=PLUGIN_CLIENT, redirect=PLUGIN_REDIRECT, resource=PLUGIN
        )
        access = tokens["access_token"]
        info = await http.get("/oauth/userinfo", headers=bearer(access))
        models = await http.get("/v1/models", headers=bearer(access))
        chat = await http.post(
            "/v1/chat/completions",
            json={"model": "mia:default", "messages": [{"role": "user", "content": "hi"}]},
            headers=bearer(access),
        )
        mcp = await mcp_list(http, access)
        # the plugin's own sign-in token (no resource) has no audience but says who it is for
        plain = await world.login(http)
        plain_info = (
            await http.get("/oauth/userinfo", headers=bearer(plain["access_token"]))
        ).json()
    assert info.status_code == 200, info.text
    body = info.json()
    assert body["aud"] == PLUGIN and body["azp"] == PLUGIN_CLIENT and body["sub"]
    assert body["email"].endswith("@example.test")
    for refused in (models, chat, mcp):
        assert refused.status_code == 401, refused.text
        assert 'error="invalid_token"' in refused.headers["www-authenticate"]
    assert "aud" not in plain_info and plain_info["azp"] == CLIENT_ID


# -- R3: minted tokens for `mia_token` plugins ---------------------------------------------------


async def grant_for(world: OAuthWorld, user: uuid.UUID, client_id: str, scopes: str) -> uuid.UUID:
    grant_id = uuid.uuid4()
    async with world.factory() as session, session.begin():
        session.add(
            OAuthGrant(
                id=grant_id,
                user_id=user,
                client_id=client_id,
                client_name="Maps",
                scopes=scopes,
                monthly_limit_usd=5,
            )
        )
    return grant_id


async def test_a_minted_token_comes_from_the_grant_of_that_client_and_never_exceeds_it(
    world: OAuthWorld,
) -> None:
    service = world.stack.service
    user, other = world.users
    await grant_for(world, user, PLUGIN_CLIENT, "openid email")
    await grant_for(world, user, CLIENT_ID, "openid llm.generate")  # another app's grant

    minted = await service.mint_plugin_token(
        user, client_id=PLUGIN_CLIENT, resource=PLUGIN, scopes=("openid", "llm.generate", "email")
    )
    assert minted is not None
    assert minted.scopes == ("openid", "email")  # llm.generate is not in this grant
    assert minted.expires_at - utc_now() <= timedelta(seconds=300)
    access = await service.authenticate(minted.access_token)
    assert access is not None
    assert (access.client_id, access.resource, access.user_id) == (PLUGIN_CLIENT, PLUGIN, user)
    assert access.scopes == frozenset({"openid", "email"})
    async with world.factory() as session:
        rows = (
            await session.scalars(select(OAuthToken).where(OAuthToken.resource == PLUGIN))
        ).all()
    assert [row.kind for row in rows] == ["access"]  # no refresh token, ever

    # no grant of this client for that user, or nothing in common: nothing is minted
    assert await service.mint_plugin_token(other, client_id=PLUGIN_CLIENT, resource=PLUGIN) is None
    assert (
        await service.mint_plugin_token(
            user, client_id=PLUGIN_CLIENT, resource=PLUGIN, scopes=("memory.read:home",)
        )
        is None
    )
    assert (
        await service.mint_plugin_token(
            user, client_id="https://maps.example/x.json", resource=PLUGIN
        )
        is None
    )


async def test_the_ttl_is_capped_at_five_minutes_and_by_the_access_ttl(world: OAuthWorld) -> None:
    service = world.stack.service
    await grant_for(world, world.user, PLUGIN_CLIENT, "openid")
    service.config = OAuthConfig(
        issuer=ISSUER, consent_url=CONSENT, access_ttl_s=120, plugin_token_ttl_s=300
    )
    minted = await service.mint_plugin_token(world.user, client_id=PLUGIN_CLIENT, resource=PLUGIN)
    assert minted is not None
    assert minted.expires_at - utc_now() <= timedelta(seconds=120)
    service.clock = lambda: utc_now() + timedelta(seconds=121)
    assert await service.authenticate(minted.access_token) is None


async def test_revoking_the_grant_kills_minted_tokens_and_stops_minting(world: OAuthWorld) -> None:
    service = world.stack.service
    grant = await grant_for(world, world.user, PLUGIN_CLIENT, "openid")
    minted = await service.mint_plugin_token(world.user, client_id=PLUGIN_CLIENT, resource=PLUGIN)
    assert minted is not None
    async with world.http() as http:
        info = await http.get("/oauth/userinfo", headers=bearer(minted.access_token))
        assert info.status_code == 200 and info.json()["aud"] == PLUGIN
        assert info.json()["azp"] == PLUGIN_CLIENT
        assert (
            await http.get("/v1/models", headers=bearer(minted.access_token))
        ).status_code == 401
        assert (await mcp_list(http, minted.access_token)).status_code == 401

        await service.revoke_grant(world.user, grant)

        after = await http.get("/oauth/userinfo", headers=bearer(minted.access_token))
    assert after.status_code == 401
    assert await service.authenticate(minted.access_token) is None
    assert (
        await service.mint_plugin_token(world.user, client_id=PLUGIN_CLIENT, resource=PLUGIN)
        is None
    )


async def test_removing_openid_from_the_grant_stops_minting(world: OAuthWorld) -> None:
    service = world.stack.service
    grant = await grant_for(world, world.user, PLUGIN_CLIENT, "openid email")
    await service.update_grant(world.user, grant, remove_scopes=["openid"])
    assert (
        await service.mint_plugin_token(world.user, client_id=PLUGIN_CLIENT, resource=PLUGIN)
        is None
    )
    view = await service.plugin_grant(world.user, PLUGIN_CLIENT)
    assert view is not None and view.scopes == ("email",)
    async with world.factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM oauth_tokens"))).scalar() == 0
