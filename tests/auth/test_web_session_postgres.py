"""Web cookie session and Android bearer resolve to one MIA user (M10)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from app.modules.auth.models import WebSession
from app.modules.users.models import User
from sqlalchemy import func, select

from tests.workspace.harness import (
    ORIGIN,
    Harness,
    android_login,
    build_harness,
    key,
    requires_postgres,
    web_headers,
    web_login,
)

pytestmark = requires_postgres


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async with build_harness() as value:
        yield value


async def test_cookie_and_bearer_yield_the_same_user_and_workspace(harness: Harness) -> None:
    async with harness.client() as android, harness.client() as browser:
        bearer = await android_login(android, "sub-alice")
        signed_in = await web_login(browser, "sub-alice")

        via_bearer = await android.get("/v1/me", headers=bearer)
        via_cookie = await browser.get("/v1/me")
        assert via_bearer.status_code == via_cookie.status_code == 200
        assert via_bearer.json()["id"] == via_cookie.json()["id"] == signed_in["user"]["id"]

        workspace_bearer = await android.get("/v1/workspace", headers=bearer)
        workspace_cookie = await browser.get("/v1/workspace")
        assert workspace_bearer.json() == workspace_cookie.json()
        assert workspace_bearer.json()["owner_user_id"] == via_bearer.json()["id"]

    async with harness.factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_accounts_link_by_google_sub_never_by_email(harness: Harness) -> None:
    async with harness.client() as android, harness.client() as browser:
        bearer = await android_login(android, "sub-one", "shared@example.test")
        await web_login(browser, "sub-two", "shared@example.test")
        first = (await android.get("/v1/me", headers=bearer)).json()
        second = (await browser.get("/v1/me")).json()
    assert first["email"] == second["email"]
    assert first["id"] != second["id"]


async def test_session_cookie_is_httponly_strict_and_token_is_hashed(harness: Harness) -> None:
    async with harness.client() as browser:
        nonce = await browser.post("/v1/auth/web/nonce", headers=key())
        assert "HttpOnly" in nonce.headers["set-cookie"]
        login = await browser.post(
            "/v1/auth/web/google",
            headers={**key(), "Origin": ORIGIN},
            json={"credential": "google:sub-cookie"},
        )
        assert login.status_code == 200
        cookie_header = ";".join(login.headers.get_list("set-cookie"))
        assert "mia_session=" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "samesite=strict" in cookie_header.lower()
        token = browser.cookies["mia_session"]
        assert token not in login.text
    async with harness.factory() as session:
        stored = list(await session.scalars(select(WebSession.token_hash)))
    assert stored and token not in stored


async def test_web_sign_in_requires_the_browser_bound_nonce(harness: Harness) -> None:
    async with harness.client() as browser, harness.client() as attacker:
        await browser.post("/v1/auth/web/nonce", headers=key())
        # No nonce cookie in this jar: login CSRF / token injection is refused.
        refused = await attacker.post(
            "/v1/auth/web/google", headers=key(), json={"credential": "google:sub-x"}
        )
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] == "invalid_credentials"

        nonce = browser.cookies["mia_web_nonce"]
        first = await browser.post(
            "/v1/auth/web/google", headers=key(), json={"credential": "google:sub-x"}
        )
        assert first.status_code == 200
        browser.cookies.set("mia_web_nonce", nonce)
        replay = await browser.post(
            "/v1/auth/web/google", headers=key(), json={"credential": "google:sub-x"}
        )
        assert replay.status_code == 401


async def test_cross_site_cookie_mutations_are_rejected(harness: Harness) -> None:
    async with harness.client() as browser:
        await web_login(browser, "sub-csrf")
        body = {
            "plugin_id": "mia.timer",
            "version": "1.0.0",
            "tools_digest": "sha256:timer-tools-v1",
        }
        for headers in (
            {**key(), "Origin": "https://evil.example"},
            {**key(), "Sec-Fetch-Site": "cross-site"},
            {**key(), "Sec-Fetch-Site": "same-site", "Origin": "https://sub.mia.test"},
        ):
            denied = await browser.post("/v1/installations", headers=headers, json=body)
            assert denied.status_code == 403
            assert denied.json()["error"]["code"] == "permission_denied"
        allowed = await browser.post("/v1/installations", headers=web_headers(), json=body)
        assert allowed.status_code == 201
        # Reads are not subject to the origin check.
        listed = await browser.get("/v1/installations", headers={"Origin": "https://evil.example"})
        assert listed.status_code == 200


async def test_web_logout_revokes_only_that_browser(harness: Harness) -> None:
    async with harness.client() as first, harness.client() as second:
        await web_login(first, "sub-logout")
        await web_login(second, "sub-logout")
        logout = await first.post("/v1/auth/web/logout", headers=web_headers())
        assert logout.status_code == 204
        assert (await first.get("/v1/me")).status_code == 401
        assert (await second.get("/v1/me")).status_code == 200


async def test_deleted_account_cannot_use_its_cookie(harness: Harness) -> None:
    async with harness.client() as browser:
        signed_in = await web_login(browser, "sub-deleted")
        async with harness.factory() as session, session.begin():
            user = await session.get(User, UUID(signed_in["user"]["id"]))
            assert user is not None
            user.deleted_at = datetime.now(UTC)
        denied = await browser.get("/v1/me")
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "invalid_credentials"
