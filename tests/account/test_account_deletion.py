"""``DELETE /v1/account`` (M17-R1) on the production composition (PostgreSQL + Redis).

What deletion must leave behind: nothing of the account in any table, dead credentials (Android
bearer, browser cookie, Mia sign-in tokens of apps), plugin tokens revoked at the provider
(OI-M15-006), a shredded data key, one journal row without content — and a fresh, empty
account on the next Google sign-in.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
from app.modules.oauth.models import OAuthGrant
from sqlalchemy.exc import OperationalError

from tests.e2e_mvp.harness import PUBLIC_BASE, Mvp, Person, call, idem, requires_stack
from tests.e2e_mvp.s04_support import linear_connected, link_telegram, push_token

pytestmark = requires_stack

APP_CLIENT = "https://maps.example/oauth/client.json"
SAME_ORIGIN = {"Origin": PUBLIC_BASE, "Sec-Fetch-Site": "same-origin"}


async def rows_left(mvp: Mvp, user_id: str, workspace_id: uuid.UUID | None) -> dict[str, int]:
    """Every table with a user or workspace column: rows that still point at the account."""
    columns = await mvp.sql(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type = 'uuid' "
        "AND column_name IN ('user_id', 'owner_user_id', 'workspace_id') "
        "AND table_name <> 'account_deletions'"
    )
    left: dict[str, int] = {}
    for table, column in columns:
        value = workspace_id if column == "workspace_id" else uuid.UUID(user_id)
        if value is None:
            continue
        [row] = await mvp.sql(
            f'SELECT count(*) AS n FROM "{table}" WHERE "{column}" = :v',  # noqa: S608
            v=value,
        )
        if row.n:
            left[f"{table}.{column}"] = row.n
    return left


async def app_token(mvp: Mvp, user_id: str) -> str:
    """«Войти через Mia» in a third-party app: a grant and a token minted from it."""
    async with mvp.factory() as session, session.begin():
        session.add(
            OAuthGrant(
                id=uuid.uuid4(),
                user_id=uuid.UUID(user_id),
                client_id=APP_CLIENT,
                client_name="Maps",
                scopes="openid email",
                monthly_limit_usd=5,
            )
        )
    minted = await mvp.app.state.oauth.service.mint_plugin_token(
        uuid.UUID(user_id), client_id=APP_CLIENT, resource="https://maps.example/mcp"
    )
    assert minted is not None
    assert await mvp.app.state.oauth.service.authenticate(minted.access_token) is not None
    return str(minted.access_token)


def provider_tokens_alive(mvp: Mvp) -> list[str]:
    """Tokens the fake provider issued to Mia for the user (not the publisher's own)."""
    return [
        token
        for token, record in mvp.auth_server.access.items()
        if not record["revoked"]
        and mvp.auth_server.refresh[record["refresh"]]["client_id"] != "publisher"
    ]


async def workspace_of(mvp: Mvp, user_id: str) -> uuid.UUID:
    [row] = await mvp.sql(
        "SELECT id FROM workspaces WHERE owner_user_id = :u", u=uuid.UUID(user_id)
    )
    return uuid.UUID(str(row.id))


async def journal(mvp: Mvp, user_id: str) -> list[Any]:
    return await mvp.sql("SELECT * FROM account_deletions WHERE user_id = :u", u=uuid.UUID(user_id))


async def test_deleting_the_account_from_the_phone_removes_everything(
    mvp: Mvp, tmp_path: Path
) -> None:
    phone = await mvp.phone("erin")
    web = await mvp.browser("erin")
    assert web.user_id == phone.user_id
    old_cookie = web.http.cookies.get("mia_session")
    assert old_cookie

    # A lived-in account: memory, a plugin account at a provider, push, Telegram, an app.
    fact = await phone.post("/v1/memory/facts", {"text": "Живёт в Минске", "category": "profile"})
    assert fact.status_code == 201, fact.text
    await linear_connected(mvp, phone, tmp_path)
    assert provider_tokens_alive(mvp)
    await push_token(phone, "fcm-token-erin")
    await link_telegram(mvp, phone)
    token = await app_token(mvp, phone.user_id)
    workspace = await workspace_of(mvp, phone.user_id)
    assert await rows_left(mvp, phone.user_id, workspace)

    deleted = await phone.delete("/v1/account")
    assert deleted.status_code == 204, deleted.text

    # Every credential of the account is dead.
    assert (await phone.get("/v1/workspace")).status_code == 401
    assert (await web.get("/v1/workspace")).status_code == 401
    replay = await mvp.client.get("/v1/workspace", headers={"Cookie": f"mia_session={old_cookie}"})
    assert replay.status_code == 401
    assert await mvp.app.state.oauth.service.authenticate(token) is None
    # The plugin account was revoked at the provider (OI-M15-006).
    assert mvp.auth_server.revocations
    assert provider_tokens_alive(mvp) == []

    # Nothing of the account is left in any table; the data key is gone.
    assert await rows_left(mvp, phone.user_id, workspace) == {}
    [users] = await mvp.sql(
        "SELECT count(*) AS n FROM users WHERE id = :u", u=uuid.UUID(phone.user_id)
    )
    assert users.n == 0

    # The journal records the deletion without content.
    [entry] = await journal(mvp, phone.user_id)
    assert entry.requested_via == "bearer"
    assert entry.provider_connections == 1
    assert entry.memory_shredded is True
    assert set(entry._mapping) == {
        "id",
        "user_id",
        "deleted_at",
        "requested_via",
        "provider_connections",
        "memory_shredded",
    }

    # A new Google sign-in creates a new, empty account.
    again = await mvp.phone("erin")
    assert again.user_id != phone.user_id
    assert (await again.get("/v1/memory/facts")).json()["items"] == []
    assert (await again.get("/v1/installations")).json()["items"] == []
    assert (await again.get("/v1/telegram/link")).json()["linked"] is False


async def test_deleting_from_the_browser_needs_same_origin_and_clears_the_cookie(
    mvp: Mvp,
) -> None:
    web = await mvp.browser("fay")
    cookie = web.http.cookies.get("mia_session")
    assert cookie

    # Cross-site: refused, the account stays.
    cross = await web.http.delete(
        "/v1/account",
        headers={**idem(), "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert cross.status_code == 403, cross.text
    # Without Idempotency-Key: refused like every mutation.
    bare = await web.http.delete("/v1/account", headers=SAME_ORIGIN)
    assert bare.status_code == 422, bare.text
    assert (await web.get("/v1/workspace")).status_code == 200

    deleted = await web.delete("/v1/account")
    assert deleted.status_code == 204, deleted.text
    assert "mia_session=" in deleted.headers.get("set-cookie", "")
    assert "Max-Age=0" in deleted.headers["set-cookie"]
    [entry] = await journal(mvp, web.user_id)
    assert entry.requested_via == "cookie"
    assert entry.provider_connections == 0

    # The old cookie value is dead even if a client kept it; a retry answers 401.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mvp.app), base_url=PUBLIC_BASE
    ) as browser:
        browser.cookies.set("mia_session", cookie)
        assert (await browser.get("/v1/workspace")).status_code == 401
        retry = await browser.delete("/v1/account", headers={**idem(), **SAME_ORIGIN})
        assert retry.status_code == 401


async def test_anonymous_deletion_is_refused(mvp: Mvp) -> None:
    response = await mvp.client.delete("/v1/account", headers=idem())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


async def test_a_run_in_progress_is_stopped_before_the_account_goes(mvp: Mvp) -> None:
    phone = await mvp.phone("gus")
    await phone.register_device_tools()
    mvp.model.then(call("device.contacts.search", {"query": "Яна"}))
    run_id = await phone.start_run("Найди Яну в контактах", profile="voice")
    await mvp.wait_event(phone.user_id, run_id, "device_tool.call", wait_s=15)
    assert mvp.runs.is_live(uuid.UUID(run_id))

    deleted = await phone.delete("/v1/account")
    assert deleted.status_code == 204, deleted.text
    assert not mvp.runs.is_live(uuid.UUID(run_id))
    [runs] = await mvp.sql("SELECT count(*) AS n FROM runs WHERE id = :r", r=uuid.UUID(run_id))
    assert runs.n == 0


class FailingShredder:
    async def shred_account(self, *, user_id: str) -> bool:
        raise OperationalError("DELETE", {}, Exception("connection lost"))


async def test_a_failed_step_keeps_the_account_and_can_be_retried(mvp: Mvp) -> None:
    phone: Person = await mvp.phone("hal")
    service = mvp.app.state.account
    shredder = service._memory  # noqa: SLF001
    service._memory = FailingShredder()  # noqa: SLF001
    try:
        failed = await phone.delete("/v1/account")
    finally:
        service._memory = shredder  # noqa: SLF001
    assert failed.status_code == 503, failed.text
    error = failed.json()["error"]
    assert error["code"] == "account_deletion_incomplete" and error["retryable"] is True
    assert (await phone.get("/v1/workspace")).status_code == 200
    assert await journal(mvp, phone.user_id) == []

    deleted = await phone.delete("/v1/account")
    assert deleted.status_code == 204, deleted.text
    assert (await phone.get("/v1/workspace")).status_code == 401
