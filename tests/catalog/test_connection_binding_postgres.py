"""OI-M15-003: a web OAuth flow is bound to the browser that started it (Lax cookie)."""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx
import pytest
from app.api.v1.plugins import BINDING_COOKIE
from httpx import ASGITransport, AsyncClient

from tests.catalog.harness import Store, build_store
from tests.catalog.remote_world import RESOURCE, browser_authorize
from tests.runs.conftest import requires_postgres
from tests.workspace.harness import BASE_URL, key, web_headers, web_login

pytestmark = requires_postgres


@pytest.fixture
async def store():  # type: ignore[no-untyped-def]
    async with build_store() as value:
        yield value


def browser(store: Store) -> AsyncClient:
    """A separate cookie jar, like another browser."""
    return AsyncClient(transport=ASGITransport(app=store.app), base_url=BASE_URL)


async def start(
    client: AsyncClient, headers: dict[str, str], installation_id: str, idem: str | None = None
) -> httpx.Response:
    return await client.post(
        "/v1/connections",
        headers={**headers, **({"Idempotency-Key": idem} if idem else {})},
        json={"installation_id": installation_id, "return_to": "/store/connected"},
    )


async def consent_location(store: Store, started: httpx.Response) -> str:
    assert started.status_code == 201, started.text
    location = await browser_authorize(store.remote, started.json()["authorization_url"])
    parts = urllib.parse.urlsplit(location)
    return f"{parts.path}?{parts.query}"


async def custom(client: AsyncClient, headers: dict[str, str]) -> str:
    created = await client.post(
        "/v1/installations", headers=headers, json={"custom_mcp_url": RESOURCE}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def connection_status(client: AsyncClient) -> Any:
    items = (await client.get("/v1/connections")).json()["items"]
    return items[0]["status"] if items else None


async def test_the_callback_needs_the_browser_that_started_the_flow(store: Store) -> None:
    async with browser(store) as owner, browser(store) as victim:
        await web_login(owner, "binding-owner")
        await web_login(victim, "binding-victim")
        installation = await custom(owner, web_headers())

        started = await start(owner, web_headers(), installation)
        cookie = started.headers["set-cookie"]
        assert f"{BINDING_COOKIE}=" in cookie
        assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
        assert "Path=/v1/connections/callback" in cookie
        callback = await consent_location(store, started)

        # Someone else's browser (no cookie) follows the same link: refused, state burnt.
        refused = await victim.get(callback)
        assert refused.status_code == 303
        assert refused.headers["location"] == (
            "/store/connected?connection=error&code=connection_flow_invalid"
        )
        assert await connection_status(owner) == "pending"
        # The link cannot be replayed by the real owner either: it is single use.
        replay = await owner.get(callback)
        assert replay.status_code == 400


async def test_the_owners_browser_completes_and_the_cookie_is_cleared(store: Store) -> None:
    async with browser(store) as owner:
        await web_login(owner, "binding-ok")
        installation = await custom(owner, web_headers())
        callback = await consent_location(store, await start(owner, web_headers(), installation))
        done = await owner.get(callback)
        assert done.headers["location"] == "/store/connected?connection=ok"
        assert f"{BINDING_COOKIE}=" in done.headers["set-cookie"]  # deleted
        assert await connection_status(owner) == "connected"


async def test_a_retried_start_keeps_the_same_binding(store: Store) -> None:
    async with browser(store) as owner:
        await web_login(owner, "binding-retry")
        installation = await custom(owner, web_headers())
        headers = web_headers()
        first = await start(owner, headers, installation)
        second = await start(owner, headers, installation)
        assert second.status_code == 201
        assert (
            first.headers["set-cookie"].split(";")[0] == second.headers["set-cookie"].split(";")[0]
        )
        callback = await consent_location(store, second)
        assert (await owner.get(callback)).headers["location"].endswith("connection=ok")


async def test_a_wrong_cookie_value_is_refused(store: Store) -> None:
    async with browser(store) as owner:
        await web_login(owner, "binding-wrong")
        installation = await custom(owner, web_headers())
        callback = await consent_location(store, await start(owner, web_headers(), installation))
        owner.cookies.set(BINDING_COOKIE, "forged", path="/v1/connections/callback")
        refused = await owner.get(callback)
        assert refused.headers["location"].endswith("code=connection_flow_invalid")


async def test_bearer_flows_stay_unbound(store: Store) -> None:
    user = await store.login("binding-android")
    created = await store.post(user, "/v1/installations", {"custom_mcp_url": RESOURCE})
    started = await store.client.post(
        "/v1/connections",
        headers={**user, **key()},
        json={"installation_id": created.json()["id"], "return_to": "/done"},
    )
    assert started.status_code == 201 and "set-cookie" not in started.headers
    callback = await consent_location(store, started)
    async with browser(store) as other:  # no cookie, no session: still completes
        assert (await other.get(callback)).headers["location"] == "/done?connection=ok"
