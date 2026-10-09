"""The Mia bot: deep-link binding, webhook secret, link API, user isolation (Postgres)."""

from __future__ import annotations

import hmac
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.api.v1 import telegram as telegram_api
from app.modules.notifications.channels import ChannelRegistry
from app.modules.notifications.telegram import (
    REPLY_BAD_TOKEN,
    REPLY_HELP,
    REPLY_LINKED,
    REPLY_NO_TOKEN,
    REPLY_UNLINKED,
    TelegramStack,
    hash_token,
    install_telegram,
)
from app.modules.notifications.telegram_models import TelegramLinkRow, TelegramLinkTokenRow
from httpx import AsyncClient
from sqlalchemy import select, text

from tests.plugins.telegram.conftest import (
    WEBHOOK_SECRET,
    FakeBotApi,
    _Runtime,
    requires_postgres,
    telegram_settings,
)
from tests.workspace.harness import Harness, android_login, build_harness, key

pytestmark = requires_postgres
HEADER = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}


@dataclass
class Bot:
    harness: Harness
    api: FakeBotApi
    stack: TelegramStack
    channels: ChannelRegistry


@asynccontextmanager
async def bot_app(*, fake_api: bool = True, **settings: Any) -> AsyncIterator[Bot]:
    async with build_harness() as harness:
        api, channels = FakeBotApi(), ChannelRegistry()
        stack = install_telegram(
            harness.app,
            telegram_settings(**settings),
            _Runtime(harness.factory),  # type: ignore[arg-type]
            channels=channels,
            api=api if fake_api else None,
        )
        yield Bot(harness, api, stack, channels)


def start_update(
    token: str | None, *, chat: int = 4242, user: int = 777, kind: str = "private"
) -> dict[str, Any]:
    text_value = "/start" if token is None else f"/start {token}"
    return message_update(text_value, chat=chat, user=user, kind=kind)


def message_update(
    text_value: str,
    *,
    chat: int = 4242,
    user: int = 777,
    kind: str = "private",
    is_bot: bool = False,
) -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "date": 1_800_000_000,
            "chat": {"id": chat, "type": kind},
            "from": {"id": user, "is_bot": is_bot, "first_name": "Aleksey"},
            "text": text_value,
        },
    }


async def webhook(client: AsyncClient, body: Any, headers: dict[str, str] | None = HEADER) -> Any:
    return await client.post("/webhooks/telegram", json=body, headers=headers or {})


async def new_link(client: AsyncClient, auth: dict[str, str]) -> tuple[str, dict[str, Any]]:
    created = await client.post("/v1/telegram/link", headers={**auth, **key()})
    assert created.status_code == 201, created.text
    body = created.json()
    token = re.fullmatch(
        r"https://t\.me/mia_test_bot\?start=([A-Za-z0-9_-]{43})", body["deep_link"]
    )
    assert token is not None, body
    return token.group(1), body


async def test_deep_link_binds_the_chat_once() -> None:
    async with bot_app() as ctx, ctx.harness.client() as client:
        auth = await android_login(client, "owner")
        before = (await client.get("/v1/telegram/link", headers=auth)).json()
        assert before == {
            "available": True,
            "linked": False,
            "bot_username": "mia_test_bot",
            "linked_at": None,
        }
        token, body = await new_link(client, auth)
        assert datetime.fromisoformat(body["expires_at"]) > datetime.now(UTC)

        async with ctx.harness.factory() as session:
            stored = (await session.scalars(select(TelegramLinkTokenRow))).all()
            assert [row.token_hash for row in stored] == [hash_token(token)]
            assert token not in stored[0].token_hash  # only the hash is stored

        answered = await webhook(client, start_update(token))
        assert answered.status_code == 200 and answered.json() == {"ok": True}
        assert ctx.api.sent == [(4242, REPLY_LINKED)]
        after = (await client.get("/v1/telegram/link", headers=auth)).json()
        assert after["linked"] is True and after["linked_at"] is not None

        # The token works once: a second chat cannot reuse it.
        await webhook(client, start_update(token, chat=9999, user=888))
        assert ctx.api.sent[-1] == (9999, REPLY_BAD_TOKEN)
        async with ctx.harness.factory() as session:
            links = (await session.scalars(select(TelegramLinkRow))).all()
            assert [(row.chat_id, row.telegram_user_id) for row in links] == [(4242, 777)]


async def test_expired_unknown_and_malformed_tokens_do_not_bind() -> None:
    async with bot_app() as ctx, ctx.harness.client() as client:
        auth = await android_login(client, "owner")
        token, _ = await new_link(client, auth)
        async with ctx.harness.factory() as session, session.begin():
            await session.execute(
                text("UPDATE telegram_link_tokens SET expires_at = :t"),
                {"t": datetime.now(UTC) - timedelta(minutes=1)},
            )
        for candidate in (token, "A" * 43, "short", "bad token with spaces"):
            await webhook(client, start_update(candidate))
            assert ctx.api.sent[-1] == (4242, REPLY_BAD_TOKEN)
        assert (await client.get("/v1/telegram/link", headers=auth)).json()["linked"] is False


async def test_webhook_rejects_a_missing_or_wrong_secret() -> None:
    async with bot_app() as ctx, ctx.harness.client() as client:
        auth = await android_login(client, "owner")
        token, _ = await new_link(client, auth)
        for headers in (
            None,
            {"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET[:-1]},
            {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET + "x"},
        ):
            denied = await webhook(client, start_update(token), headers=headers or {"X": "y"})
            assert denied.status_code == 403
            assert denied.json()["error"]["code"] == "telegram_webhook_forbidden"
        assert ctx.api.sent == []
        assert (await client.get("/v1/telegram/link", headers=auth)).json()["linked"] is False


def test_secret_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return bool(real(a, b))

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert telegram_api.secret_matches("abc", "abc") is True
    assert telegram_api.secret_matches("abc", "abd") is False
    assert telegram_api.secret_matches("abc", "") is False
    assert telegram_api.secret_matches("", "") is False
    assert telegram_api.secret_matches("abc", None) is False
    assert len(calls) == 2


async def test_webhook_without_configuration_is_off() -> None:
    async with bot_app(
        fake_api=False, telegram_bot_token=None, telegram_webhook_secret=None
    ) as ctx:
        async with ctx.harness.client() as client:
            auth = await android_login(client, "owner")
            off = await webhook(client, start_update("x" * 43))
            assert off.status_code == 503
            assert off.json()["error"]["code"] == "telegram_not_configured"
            status = await client.get("/v1/telegram/link", headers=auth)
            assert status.json()["available"] is False
            link = await client.post("/v1/telegram/link", headers={**auth, **key()})
            assert link.status_code == 503
            assert link.json()["error"]["code"] == "telegram_not_configured"
        assert ctx.channels.get("telegram") is None


async def test_link_endpoints_need_a_signed_in_user_and_isolate_users() -> None:
    async with bot_app() as ctx, ctx.harness.client() as client, ctx.harness.client() as other:
        assert (await client.get("/v1/telegram/link")).status_code == 401
        assert (await client.post("/v1/telegram/link", headers=key())).status_code == 401
        auth = await android_login(client, "owner")
        stranger = await android_login(other, "stranger")
        token, _ = await new_link(client, auth)
        await webhook(client, start_update(token))
        assert (await client.get("/v1/telegram/link", headers=auth)).json()["linked"] is True
        assert (await other.get("/v1/telegram/link", headers=stranger)).json()["linked"] is False

        # Unlinking is per user and idempotent.
        assert (
            await other.delete("/v1/telegram/link", headers={**stranger, **key()})
        ).status_code == 204
        assert (await client.get("/v1/telegram/link", headers=auth)).json()["linked"] is True
        assert (
            await client.delete("/v1/telegram/link", headers={**auth, **key()})
        ).status_code == 204
        assert (
            await client.delete("/v1/telegram/link", headers={**auth, **key()})
        ).status_code == 204
        assert (await client.get("/v1/telegram/link", headers=auth)).json()["linked"] is False


async def test_relinking_moves_the_chat_and_a_chat_belongs_to_one_user() -> None:
    async with bot_app() as ctx, ctx.harness.client() as client, ctx.harness.client() as other:
        first = await android_login(client, "owner")
        second = await android_login(other, "stranger")
        token_a, _ = await new_link(client, first)
        await webhook(client, start_update(token_a, chat=1111))
        token_b, _ = await new_link(other, second)
        await webhook(other, start_update(token_b, chat=1111, user=555))  # same chat, other user
        assert (await client.get("/v1/telegram/link", headers=first)).json()["linked"] is False
        assert (await other.get("/v1/telegram/link", headers=second)).json()["linked"] is True
        token_c, _ = await new_link(other, second)
        await webhook(other, start_update(token_c, chat=2222, user=555))  # same user, new chat
        async with ctx.harness.factory() as session:
            rows = (await session.scalars(select(TelegramLinkRow))).all()
            assert [row.chat_id for row in rows] == [2222]


async def test_commands_and_noise() -> None:
    async with bot_app() as ctx, ctx.harness.client() as client:
        auth = await android_login(client, "owner")
        token, _ = await new_link(client, auth)
        await webhook(client, start_update(token))
        ctx.api.sent.clear()

        await webhook(client, message_update("/start"))
        await webhook(client, message_update("привет"))
        await webhook(client, message_update("/start@mia_test_bot"))
        assert [t for _, t in ctx.api.sent] == [REPLY_NO_TOKEN, REPLY_HELP, REPLY_NO_TOKEN]

        # Group chats, bots, edits and garbage are ignored without an error.
        ctx.api.sent.clear()
        await webhook(client, start_update("x" * 43, kind="group", chat=-100))
        await webhook(client, message_update("hi", is_bot=True))
        await webhook(client, {"update_id": 2, "edited_message": {"text": "x"}})
        await webhook(client, {"update_id": 3, "message": {"chat": "x", "from": 1}})
        await webhook(client, ["not", "an", "object"])
        garbage = await client.post("/webhooks/telegram", content=b"{broken", headers=HEADER)
        assert garbage.status_code == 200
        assert ctx.api.sent == []

        await webhook(client, message_update("/stop"))
        assert ctx.api.sent == [(4242, REPLY_UNLINKED)]
        assert (await client.get("/v1/telegram/link", headers=auth)).json()["linked"] is False


async def test_a_failing_handler_still_answers_200(monkeypatch: pytest.MonkeyPatch) -> None:
    async with bot_app() as ctx, ctx.harness.client() as client:
        assert ctx.stack.bot is not None

        async def boom(update: dict[str, Any]) -> None:
            raise RuntimeError("database down")

        monkeypatch.setattr(ctx.stack.bot, "handle_update", boom)
        answered = await webhook(client, start_update("x" * 43))
        assert answered.status_code == 200 and answered.json() == {"ok": True}
