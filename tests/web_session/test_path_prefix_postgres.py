"""Mia published under a path prefix (M62-T1): ``PUBLIC_BASE_URL=https://host/mia``.

nginx strips ``/mia`` before the app sees the request; :class:`StripPrefix` does the same here,
so the browser-side paths (cookies, redirects, hand-off URLs) are checked as a browser sees them.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.api.v1.plugins import BINDING_COOKIE
from app.api.v1.web_handoff import install_web_handoff
from app.core.config import Settings
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from tests.auth.fakes import FakeRedis
from tests.catalog.harness import PUBLIC_BASE, build_store
from tests.catalog.remote_world import RESOURCE, browser_authorize
from tests.runs.conftest import requires_postgres
from tests.workspace.harness import (
    BASE_URL,
    SECRET,
    android_login,
    build_harness,
    key,
    web_headers,
    web_login,
)

pytestmark = requires_postgres
PREFIX = "/mia"


class StripPrefix:
    """What ``location /mia/ { proxy_pass http://127.0.0.1:8004/; }`` does."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if scope["type"] == "http" and path.startswith(PREFIX + "/"):
            scope = dict(scope, path=path[len(PREFIX) :], raw_path=path[len(PREFIX) :].encode())
        await self.app(scope, receive, send)


@asynccontextmanager
async def browser(app: ASGIApp) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=StripPrefix(app)), base_url=BASE_URL + PREFIX
    ) as client:
        yield client


async def test_web_session_cookies_are_scoped_to_the_prefix() -> None:
    async with build_harness(public_base_url=BASE_URL + PREFIX) as harness:
        async with browser(harness.app) as web:
            nonce = await web.post("/v1/auth/web/nonce", headers=key())
            assert "Path=/mia/" in nonce.headers["set-cookie"]
            await web_login(web, "prefix-web")
            session = [c for c in web.cookies.jar if c.name == "mia_session"]
            assert [c.path for c in session] == ["/mia/"]
            me = await web.get("/v1/me")  # the jar sends it to /mia/v1/me only
            assert me.status_code == 200
            out = await web.post("/v1/auth/web/logout", headers=web_headers())
            assert out.status_code in {200, 204}
            assert "Path=/mia/" in out.headers["set-cookie"]


async def test_webview_hand_off_returns_prefixed_paths() -> None:
    async with build_harness(public_base_url=BASE_URL + PREFIX) as harness:
        settings = Settings(
            app_env="test",
            auth_jwt_secret=SecretStr(SECRET),
            public_base_url=BASE_URL + PREFIX,
            auth_authenticated_rate_limit=5000,
            auth_exchange_rate_limit=1000,
        )
        install_web_handoff(
            harness.app, settings, session_factory=harness.factory, redis=FakeRedis()
        )
        async with harness.client() as android, browser(harness.app) as webview:
            bearer = await android_login(android, "prefix-handoff")
            issued = await android.post(
                "/v1/auth/web/handoff", headers={**bearer, **key()}, json={"path": "/store"}
            )
            body = issued.json()
            # Android resolves it against https://host/mia: an absolute path keeps the prefix.
            assert body["redeem_path"].startswith("/mia/v1/auth/web/handoff/redeem?code=")
            redeemed = await webview.get(
                body["redeem_path"].removeprefix(PREFIX), follow_redirects=False
            )
            assert redeemed.status_code == 303
            assert redeemed.headers["location"] == "/mia/store"
            assert "Path=/mia/" in redeemed.headers["set-cookie"]
            assert (await webview.get("/v1/me")).status_code == 200


async def test_plugin_oauth_return_and_binding_cookie_keep_the_prefix() -> None:
    async with build_store(public_base_url=PUBLIC_BASE + PREFIX) as store:
        async with browser(store.app) as owner:
            await web_login(owner, "prefix-binding")
            created = await owner.post(
                "/v1/installations", headers=web_headers(), json={"custom_mcp_url": RESOURCE}
            )
            assert created.status_code == 201, created.text
            started = await owner.post(
                "/v1/connections",
                headers=web_headers(),
                json={"installation_id": created.json()["id"], "return_to": "/store/connected"},
            )
            assert started.status_code == 201, started.text
            assert "Path=/mia/v1/connections/callback" in started.headers["set-cookie"]
            location = await browser_authorize(store.remote, started.json()["authorization_url"])
            parts = urllib.parse.urlsplit(location)
            assert parts.path == "/mia/v1/connections/callback"  # PUBLIC_BASE_URL + callback
            done = await owner.get(f"{parts.path.removeprefix(PREFIX)}?{parts.query}")
            assert done.status_code == 303
            assert done.headers["location"] == "/mia/store/connected?connection=ok"
            assert f"{BINDING_COOKIE}=" in done.headers["set-cookie"]
            assert "Path=/mia/v1/connections/callback" in done.headers["set-cookie"]
