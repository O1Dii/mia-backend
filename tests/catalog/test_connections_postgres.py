"""Add-by-URL with OAuth, connection lifecycle, refresh and revocation, on Postgres.

The remote MCP server and its authorization server are in-process ASGI apps behind the real
SSRF guard; the router calls the plugin with the token Mia obtained through the full flow
(discovery, dynamic registration, PKCE S256, resource indicator, code exchange).
"""

from __future__ import annotations

import dataclasses
import urllib.parse
import uuid
from typing import Any

import httpx
import pytest
from app.core.ports.tool_router import ToolCallRequest, ToolContext
from app.modules.connections.oauth.models import PluginConnection
from sqlalchemy import select

from tests.catalog.harness import Store, build_store
from tests.catalog.remote_world import MCP_HOST, RESOURCE, browser_authorize
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture
async def store():  # type: ignore[no-untyped-def]
    async with build_store() as value:
        yield value


def _json(response: httpx.Response, status: int) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


async def custom_install(store: Store, user: dict[str, str], url: str = RESOURCE) -> dict[str, Any]:
    created = await store.post(user, "/v1/installations", {"custom_mcp_url": url})
    return dict(_json(created, 201))


async def connect_account(
    store: Store, user: dict[str, str], installation_id: str, *, return_to: str | None = "/done"
) -> httpx.Response:
    """Start the flow, let the 'browser' consent at the provider, hit our callback."""
    body: dict[str, Any] = {"installation_id": installation_id}
    if return_to is not None:
        body["return_to"] = return_to
    started = _json(await store.post(user, "/v1/connections", body), 201)
    location = await browser_authorize(store.remote, started["authorization_url"])
    parts = urllib.parse.urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://mia.test.example/v1/connections/callback"
    )
    return await store.client.get(f"{parts.path}?{parts.query}")


async def context(store: Store, user: dict[str, str]) -> ToolContext:
    workspace = _json(await store.get(user, "/v1/workspace"), 200)
    return ToolContext(
        user_id=workspace["owner_user_id"],
        run_id=str(uuid.uuid4()),
        profile="chat",
        workspace_id=workspace["id"],
    )


async def consent_custom(
    store: Store, user: dict[str, str], installation_id: str
) -> dict[str, Any]:
    found = _json(await store.get(user, f"/v1/installations/{installation_id}/discovery"), 200)
    assert found["state"] == "ready"
    current = _json(await store.get(user, f"/v1/installations/{installation_id}"), 200)
    response = await store.post(
        user,
        f"/v1/installations/{installation_id}/consent",
        {
            "expected_version": current["state_version"],
            "tools_digest": found["tools_digest"],
            "grants": [g["id"] for g in found["grants"]],
        },
    )
    return dict(_json(response, 200))


async def test_add_by_url_with_oauth_end_to_end(store: Store) -> None:
    user = await store.login("ann")
    installation = await custom_install(store, user)
    assert installation["status"] == "needs_consent" and installation["source"] == "custom"
    assert installation["plugin_id"].startswith("custom.u")
    iid = installation["id"]

    # 1. The server wants OAuth: nothing can be listed before the account is connected.
    first = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    assert (
        first["state"] == "needs_connection" and first["tools"] == [] and first["host"] == MCP_HOST
    )

    # 2. The flow: PKCE S256, the MCP URL as resource indicator, registered redirect URI.
    callback = await connect_account(store, user, iid)
    assert callback.status_code == 303
    assert callback.headers["location"] == "/done?connection=ok"
    assert callback.headers["cache-control"] == "no-store"
    assert callback.headers["referrer-policy"] == "no-referrer"
    auth = store.auth_server
    authorize = auth.authorize_requests[0]
    assert authorize["resource"] == RESOURCE and authorize["code_challenge_method"] == "S256"
    assert authorize["redirect_uri"] == "https://mia.test.example/v1/connections/callback"
    assert authorize["scope"] == "issues" and len(authorize["state"]) >= 32
    token_request = auth.token_requests[0]
    assert token_request["resource"] == RESOURCE and len(token_request["code_verifier"]) >= 43
    assert auth.registrations[0]["redirect_uris"] == [authorize["redirect_uri"]]

    # 3. The connection is listed without any secret and sealed at rest.
    listed = _json(await store.get(user, "/v1/connections"), 200)["items"]
    assert [(c["installation_id"], c["status"]) for c in listed] == [(iid, "connected")]
    assert listed[0]["account_label"] == "auth.test.example" and listed[0]["plugin_id"]
    [(access, record)] = list(auth.access.items())
    refresh = record["refresh"]
    assert access not in str(listed) and refresh not in str(listed)
    async with store.factory() as session:
        row = (await session.scalars(select(PluginConnection))).one()
    assert row.secret_ciphertext is not None
    assert access.encode() not in row.secret_ciphertext
    assert refresh.encode() not in row.secret_ciphertext
    assert row.flow_state_hash is None and row.flow_verifier_ciphertext is None
    assert row.status == "connected" and row.client_id == authorize["client_id"]

    # 4. Still needs consent: the router does not offer tools of an unconsented server.
    ctx = await context(store, user)
    assert [d.spec.name for d in await store.router.list_tools(ctx)] == []

    # 5. Probe, then consent to every tool as write_external.
    ready = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    assert ready["state"] == "ready" and ready["host"] == MCP_HOST
    assert {t["name"] for t in ready["tools"]} == {"list_issues", "create_issue", "getIssue"}
    assert {t["risk"] for t in ready["tools"]} == {"write_external"}
    assert ready["skipped_tools"] == ["getIssue"]
    assert {g["id"] for g in ready["grants"]} == {"tools.write_external", f"network:{MCP_HOST}"}
    active = await consent_custom(store, user, iid)
    assert active["status"] == "active" and active["tools_digest"] == ready["tools_digest"]
    assert set(active["grants"]) == {"tools.write_external", f"network:{MCP_HOST}"}

    # 6. The router calls the server with the bearer Mia obtained; writes need approval.
    names = {d.spec.name for d in await store.router.list_tools(ctx)}
    plugin_id = installation["plugin_id"]
    assert names == {f"{plugin_id}.list_issues", f"{plugin_id}.create_issue"}
    request = ToolCallRequest("call_1", f"{plugin_id}.create_issue", {"title": "Fix login"})
    pending = await store.router.call(ctx, request)
    assert pending.status == "approval_required" and pending.approval is not None
    assert store.remote.calls == []
    await store.tools.approvals.decide(
        user_id=ctx.user_id,
        approval_id=pending.approval.id,
        decision="approve",
        args_hash=pending.approval.args_hash,
        channel="android",
    )
    done = await store.router.call(
        ctx, dataclasses.replace(request, approval_id=pending.approval.id)
    )
    assert done.status == "succeeded" and store.remote.calls == [
        ("create_issue", {"title": "Fix login"})
    ]
    assert store.remote.mcp_requests[-1]["authorization"] == f"Bearer {access}"


async def test_tokens_refresh_before_expiry_and_rotate(store: Store) -> None:
    user = await store.login("ann")
    iid = (await custom_install(store, user))["id"]
    await connect_account(store, user, iid)
    await consent_custom(store, user, iid)
    ctx = await context(store, user)
    plugin_id = (
        await store.workspace.list_installations(await store.workspace.context_for(ctx.user_id))
    )[0].plugin_id
    [(first_access, record)] = list(store.auth_server.access.items())
    first_refresh = record["refresh"]
    async with store.factory() as session:
        before = (await session.scalars(select(PluginConnection))).one().secret_ciphertext

    store.clock.advance(3600 - 30)  # inside the 60 s refresh skew
    listed = await store.router.list_tools(ctx)
    assert {d.spec.name for d in listed} == {
        f"{plugin_id}.list_issues",
        f"{plugin_id}.create_issue",
    }

    refresh_request = store.auth_server.token_requests[-1]
    assert refresh_request["grant_type"] == "refresh_token"
    assert (
        refresh_request["refresh_token"] == first_refresh
        and refresh_request["resource"] == RESOURCE
    )
    second = [t for t in store.auth_server.access if t != first_access]
    assert len(second) == 1
    assert store.remote.mcp_requests[-1]["authorization"] == f"Bearer {second[0]}"
    async with store.factory() as session:
        row = (await session.scalars(select(PluginConnection))).one()
    assert row.secret_ciphertext != before and row.status == "connected"
    # The rotated refresh token was stored: the next refresh works too.
    store.clock.advance(3600)
    again = await store.connections.headers(
        user_id=ctx.user_id,
        installation_id=iid,
        plugin_id=plugin_id,
        auth="oauth",
    )
    assert again is not None and len(store.auth_server.access) == 3


async def test_a_rejected_refresh_token_means_connect_again(store: Store) -> None:
    user = await store.login("ann")
    iid = (await custom_install(store, user))["id"]
    await connect_account(store, user, iid)
    await consent_custom(store, user, iid)
    ctx = await context(store, user)
    store.clock.advance(7200)
    store.auth_server.refresh_failures = 1

    assert await store.router.list_tools(ctx) == []
    outcome = await store.router.call(ctx, ToolCallRequest("c", "custom.x.nope", {}))
    assert outcome.status == "failed"
    installation = _json(await store.get(user, f"/v1/installations/{iid}"), 200)
    assert installation["status"] == "needs_connection"
    connections = _json(await store.get(user, "/v1/connections"), 200)["items"]
    assert [c["status"] for c in connections] == ["expired"]
    async with store.factory() as session:
        row = (await session.scalars(select(PluginConnection))).one()
    assert row.secret_ciphertext is None
    # Connecting again repairs it and consent is still on record.
    callback = await connect_account(store, user, iid)
    assert callback.headers["location"] == "/done?connection=ok"
    assert _json(await store.get(user, f"/v1/installations/{iid}"), 200)["status"] == "active"
    assert {d.spec.name.split(".")[-1] for d in await store.router.list_tools(ctx)} == {
        "list_issues",
        "create_issue",
    }


async def test_uninstall_revokes_the_tokens_at_the_provider(store: Store) -> None:
    user = await store.login("ann")
    iid = (await custom_install(store, user))["id"]
    await connect_account(store, user, iid)
    await consent_custom(store, user, iid)
    ctx = await context(store, user)
    [(access, record)] = list(store.auth_server.access.items())
    refresh = record["refresh"]
    assert store.auth_server.access_valid(access, RESOURCE)

    response = await store.delete(user, f"/v1/installations/{iid}")
    assert response.status_code == 204

    # RFC 7009: both tokens were sent to the revocation endpoint and are dead.
    assert {(r["token"], r["token_type_hint"]) for r in store.auth_server.revocations} == {
        (refresh, "refresh_token"),
        (access, "access_token"),
    }
    assert store.auth_server.live_tokens() == []
    assert not store.auth_server.access_valid(access, RESOURCE)
    async with store.factory() as session:
        assert (await session.scalars(select(PluginConnection))).all() == []
    assert _json(await store.get(user, "/v1/connections"), 200)["items"] == []
    assert await store.router.list_tools(ctx) == []
    # The server now rejects the revoked bearer.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=store.remote), base_url=f"https://{MCP_HOST}"
    ) as raw:
        denied = await raw.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json={})
    assert denied.status_code == 401


async def test_disconnect_revokes_and_the_installation_needs_a_connection(store: Store) -> None:
    user = await store.login("ann")
    iid = (await custom_install(store, user))["id"]
    await connect_account(store, user, iid)
    await consent_custom(store, user, iid)
    [connection] = _json(await store.get(user, "/v1/connections"), 200)["items"]

    response = await store.delete(user, f"/v1/connections/{connection['id']}")
    assert response.status_code == 204
    assert store.auth_server.live_tokens() == [] and len(store.auth_server.revocations) == 2
    assert (
        _json(await store.get(user, f"/v1/installations/{iid}"), 200)["status"]
        == "needs_connection"
    )
    assert _json(await store.get(user, "/v1/connections"), 200)["items"] == []
    assert (await store.delete(user, f"/v1/connections/{connection['id']}")).status_code == 404


async def test_callback_state_is_single_use_expiring_and_unknown_states_fail(store: Store) -> None:
    user = await store.login("ann")
    iid = (await custom_install(store, user))["id"]
    started = _json(
        await store.post(user, "/v1/connections", {"installation_id": iid, "return_to": "/s?x=1"}),
        201,
    )
    location = await browser_authorize(store.remote, started["authorization_url"])
    query = urllib.parse.urlsplit(location).query

    # Plain page when no return_to is wanted; errors never echo input.
    unknown = await store.client.get("/v1/connections/callback?state=nope&code=x")
    assert unknown.status_code == 400 and "nope" not in unknown.text
    assert (await store.client.get("/v1/connections/callback")).status_code == 400

    ok = await store.client.get(f"/v1/connections/callback?{query}")
    assert ok.status_code == 303 and ok.headers["location"] == "/s?x=1&connection=ok"
    replay = await store.client.get(f"/v1/connections/callback?{query}")
    assert replay.status_code == 400  # the state was burnt by the first completion

    # Expired flow: the state is refused and cleared.
    started = _json(await store.post(user, "/v1/connections", {"installation_id": iid}), 201)
    location = await browser_authorize(store.remote, started["authorization_url"])
    store.clock.advance(601)
    late = await store.client.get(
        f"/v1/connections/callback?{urllib.parse.urlsplit(location).query}"
    )
    assert late.status_code == 400
    assert "Не удалось" in late.text


async def test_a_provider_error_or_failed_exchange_leaves_the_connection_pending(
    store: Store,
) -> None:
    user = await store.login("ann")
    iid = (await custom_install(store, user))["id"]
    started = _json(
        await store.post(user, "/v1/connections", {"installation_id": iid, "return_to": "/r"}), 201
    )
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started["authorization_url"]).query))[
        "state"
    ]
    denied = await store.client.get(f"/v1/connections/callback?state={state}&error=access_denied")
    assert denied.status_code == 303
    assert denied.headers["location"] == "/r?connection=error&code=connection_oauth_failed"
    assert store.auth_server.token_requests == []
    assert [c["status"] for c in _json(await store.get(user, "/v1/connections"), 200)["items"]] == [
        "pending"
    ]

    # A code the provider refuses (wrong code) is reported, not retried.
    started = _json(
        await store.post(user, "/v1/connections", {"installation_id": iid, "return_to": "/r"}), 201
    )
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started["authorization_url"]).query))[
        "state"
    ]
    bad = await store.client.get(f"/v1/connections/callback?state={state}&code=forged")
    assert bad.headers["location"] == "/r?connection=error&code=connection_oauth_failed"
    assert (
        _json(await store.get(user, "/v1/installations/" + iid), 200)["status"] == "needs_consent"
    )


async def test_connections_are_private_to_their_workspace(store: Store) -> None:
    ann = await store.login("ann")
    bob = await store.login("bob")
    iid = (await custom_install(store, ann))["id"]
    await connect_account(store, ann, iid)
    [connection] = _json(await store.get(ann, "/v1/connections"), 200)["items"]

    assert _json(await store.get(bob, "/v1/connections"), 200)["items"] == []
    assert (await store.delete(bob, f"/v1/connections/{connection['id']}")).status_code == 404
    assert (await store.post(bob, "/v1/connections", {"installation_id": iid})).status_code == 404
    assert (await store.get(bob, f"/v1/installations/{iid}/discovery")).status_code == 404
    assert (
        await store.post(
            bob,
            f"/v1/installations/{iid}/consent",
            {"expected_version": 0, "tools_digest": "sha256:x", "grants": []},
        )
    ).status_code == 404
    assert len(store.auth_server.revocations) == 0
    # Bob's own uninstall of a foreign id neither revokes nor deletes Ann's connection.
    assert (await store.delete(bob, f"/v1/installations/{iid}")).status_code == 404
    assert store.auth_server.revocations == []
    assert len(_json(await store.get(ann, "/v1/connections"), 200)["items"]) == 1


async def test_starting_a_flow_is_idempotent_per_key(store: Store) -> None:
    user = await store.login("ann")
    iid = (await custom_install(store, user))["id"]
    headers = {**user, "Idempotency-Key": "connect-idempotent-001"}
    first = await store.client.post(
        "/v1/connections", headers=headers, json={"installation_id": iid}
    )
    again = await store.client.post(
        "/v1/connections", headers=headers, json={"installation_id": iid}
    )
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    other = await store.post(user, "/v1/connections", {"installation_id": iid})
    assert (
        other.json()["connection_id"] == first.json()["connection_id"]
    )  # one row per installation
    assert other.json()["authorization_url"] != first.json()["authorization_url"]


async def test_return_to_must_stay_on_this_origin(store: Store) -> None:
    user = await store.login("ann")
    iid = (await custom_install(store, user))["id"]
    for bad in ("https://evil.example/x", "//evil.example/x", "/\\evil", "relative"):
        response = await store.post(
            user, "/v1/connections", {"installation_id": iid, "return_to": bad}
        )
        assert response.status_code == 422, bad


async def test_unsupported_unreachable_and_private_servers_have_clear_errors() -> None:
    async with build_store(supports_registration=False) as store:
        user = await store.login("ann")
        iid = (await custom_install(store, user))["id"]
        response = await store.post(user, "/v1/connections", {"installation_id": iid})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "connection_oauth_unsupported"
    async with build_store() as store:
        user = await store.login("ann")
        iid = (await custom_install(store, user))["id"]
        store.resolver.mapping[MCP_HOST] = ["10.1.2.3"]
        failed = await store.post(user, "/v1/connections", {"installation_id": iid})
        assert failed.status_code == 502
        assert failed.json()["error"]["code"] == "connection_oauth_failed"
        assert (await store.get(user, f"/v1/installations/{iid}/discovery")).status_code == 502
        assert store.auth_server.registrations == []


async def test_in_process_plugins_do_not_connect(store: Store) -> None:
    await store.publish_builtin("timer")
    user = await store.login("ann")
    digest = (await store.catalog.release("builtin.timer")).tools_digest
    installed = _json(
        await store.post(
            user,
            "/v1/plugins/builtin.timer/install",
            {
                "version": "1.0.0",
                "tools_digest": digest,
                "grants": (await store.catalog.release("builtin.timer")).grants,
            },
        ),
        201,
    )
    response = await store.post(user, "/v1/connections", {"installation_id": installed["id"]})
    assert response.status_code == 409
    # Uninstalling without a connection is a plain delete (the revoke hook finds nothing).
    assert (await store.delete(user, f"/v1/installations/{installed['id']}")).status_code == 204
    assert store.auth_server.revocations == []


async def test_a_registered_client_secret_is_sealed_and_used_for_refresh() -> None:
    async with build_store() as store:
        store.auth_server.confidential = "client_secret_basic"
        store.auth_server.metadata_overrides = {
            "token_endpoint_auth_methods_supported": ["client_secret_basic"]
        }
        user = await store.login("ann")
        iid = (await custom_install(store, user))["id"]
        callback = await connect_account(store, user, iid)
        assert callback.headers["location"] == "/done?connection=ok"
        [client] = store.auth_server.clients.values()
        async with store.factory() as session:
            row = (await session.scalars(select(PluginConnection))).one()
        assert row.auth_method == "client_secret_basic" and row.secret_ciphertext is not None
        assert client["secret"].encode() not in row.secret_ciphertext
        await consent_custom(store, user, iid)
        ctx = await context(store, user)
        store.clock.advance(3600)  # expired: the refresh must authenticate with the secret
        assert await store.router.list_tools(ctx)
        assert store.auth_server.token_requests[-1]["grant_type"] == "refresh_token"
        assert "client_secret" not in store.auth_server.token_requests[-1]
        # Disconnecting revokes both tokens.
        [connection] = _json(await store.get(user, "/v1/connections"), 200)["items"]
        assert (await store.delete(user, f"/v1/connections/{connection['id']}")).status_code == 204
        assert len(store.auth_server.revocations) == 2
