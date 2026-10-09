"""The Bot API client (fake transport, no network), the ``telegram`` notification channel and the
M16 path «job result → inbox → bot»."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx
import pytest
from app.modules.notifications.channels import OutboundMessage
from app.modules.notifications.models import DeliveryRow
from app.modules.notifications.telegram import TelegramChannel, TelegramLinks
from app.modules.notifications.telegram_api import (
    MAX_MESSAGE_CHARS,
    HttpTelegramBotApi,
    SendResult,
)
from app.modules.notifications.telegram_webhook import WEBHOOK_PATH, webhook_url
from sqlalchemy import select

from tests.fakes import text_step
from tests.jobs.conftest import JobsWorld, requires_postgres, world  # noqa: F401
from tests.plugins.telegram.conftest import BOT_TOKEN, FakeBotApi, telegram_settings
from tests.runs.gateways import answer, scripted

REQUESTS: list[httpx.Request] = []


def api_with(handler: Any) -> HttpTelegramBotApi:
    REQUESTS.clear()

    def record(request: httpx.Request) -> httpx.Response:
        REQUESTS.append(request)
        return handler(request)  # type: ignore[no-any-return]

    return HttpTelegramBotApi(
        BOT_TOKEN, base_url="https://bot.test", transport=httpx.MockTransport(record)
    )


async def test_send_message_calls_the_bot_api() -> None:
    api = api_with(lambda r: httpx.Response(200, json={"ok": True, "result": {}}))
    assert await api.send_message(4242, "Привет") == SendResult("sent")
    [request] = REQUESTS
    assert request.url == f"https://bot.test/bot{BOT_TOKEN}/sendMessage"
    assert json.loads(request.content) == {
        "chat_id": 4242,
        "text": "Привет",
        "disable_web_page_preview": True,
    }


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}}),
            SendResult("retry", "rate_limited", 7.0),
        ),
        (httpx.Response(502, text="bad gateway"), SendResult("retry", "telegram_unavailable")),
        (httpx.Response(403, json={"ok": False}), SendResult("blocked", "bot_blocked")),
        (
            httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"}),
            SendResult("blocked", "chat_not_found"),
        ),
        (httpx.Response(401, json={"ok": False}), SendResult("rejected", "bot_token_rejected")),
        (httpx.Response(400, json={"ok": False}), SendResult("rejected", "telegram_http_400")),
        (httpx.Response(200, text="not json"), SendResult("rejected", "telegram_http_200")),
    ],
)
async def test_bot_api_responses_are_classified(
    response: httpx.Response, expected: SendResult
) -> None:
    api = api_with(lambda r: response)
    assert await api.send_message(1, "x") == expected


async def test_network_errors_retry_and_never_log_the_token_or_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    api = api_with(fail)
    with caplog.at_level(logging.DEBUG):
        result = await api.send_message(1, "секретный текст")
    assert result == SendResult("retry", "network_error")
    assert BOT_TOKEN not in caplog.text and "секретный текст" not in caplog.text


async def test_long_text_is_truncated_to_the_telegram_limit() -> None:
    api = api_with(lambda r: httpx.Response(200, json={"ok": True}))
    await api.send_message(1, "я" * 10_000)
    assert len(json.loads(REQUESTS[0].content)["text"]) == MAX_MESSAGE_CHARS


async def test_webhook_registration_sends_the_secret_and_only_messages() -> None:
    api = api_with(lambda r: httpx.Response(200, json={"ok": True, "result": True}))
    assert await api.set_webhook("https://mia.test/webhooks/telegram", "sec_ret-1") is True
    assert REQUESTS[0].url.path.endswith("/setWebhook")
    assert json.loads(REQUESTS[0].content) == {
        "url": "https://mia.test/webhooks/telegram",
        "secret_token": "sec_ret-1",
        "allowed_updates": ["message"],
    }
    assert (
        await api_with(lambda r: httpx.Response(401, json={"ok": False})).set_webhook("u", "s")
        is False
    )
    assert await api.delete_webhook() is True


def test_webhook_url_uses_the_public_base_url() -> None:
    settings = telegram_settings(public_base_url="https://mia.example.org/")
    assert webhook_url(settings) == "https://mia.example.org" + WEBHOOK_PATH


def outbound(user: uuid.UUID, title: str = "Сводка", text: str = "Три новости") -> OutboundMessage:
    return OutboundMessage(
        delivery_id=uuid.uuid4(),
        user_id=user,
        inbox_item_id=uuid.uuid4(),
        source_type="job",
        source_id="j",
        title=title,
        text=text,
        response={"speech": text},
        attempt=1,
        channel="telegram",
    )


@requires_postgres
async def test_channel_delivery_outcomes(world: JobsWorld) -> None:  # noqa: F811
    links, bot = TelegramLinks(world.factory), FakeBotApi()
    channel = TelegramChannel(links, bot)

    # No linked chat: skipped, nothing sent.
    skipped = await channel.deliver(outbound(world.user))
    assert (skipped.status, skipped.error_code) == ("skipped", "channel_not_connected")
    assert bot.sent == []

    offer = await links.create_offer(world.user)
    assert await links.bind(token=offer.token, chat_id=555, telegram_user_id=9) == world.user
    assert (await channel.deliver(outbound(world.user))).status == "sent"
    assert (await channel.deliver(outbound(world.user, "Одно", "Одно"))).status == "sent"
    assert bot.sent == [(555, "Сводка\n\nТри новости"), (555, "Одно")]

    bot.outcomes += [
        SendResult("retry", "rate_limited"),
        SendResult("rejected", "telegram_http_400"),
    ]
    assert (await channel.deliver(outbound(world.user))).status == "retry"
    rejected = await channel.deliver(outbound(world.user))
    assert (rejected.status, rejected.error_code) == ("failed", "telegram_http_400")
    assert await links.chat_for(world.user) is not None

    # The user blocked the bot: the delivery fails and the dead chat is unlinked.
    bot.outcomes.append(SendResult("blocked", "bot_blocked"))
    blocked = await channel.deliver(outbound(world.user))
    assert (blocked.status, blocked.error_code) == ("failed", "bot_blocked")
    assert await links.chat_for(world.user) is None
    assert (await channel.deliver(outbound(world.user))).status == "skipped"


@requires_postgres
async def test_job_result_reaches_the_user_through_the_bot(world: JobsWorld) -> None:  # noqa: F811
    gateway = scripted(text_step(answer("Сводка готова: три новости.")))
    stack = world.stack(world.run_service(gateway), push=False)
    links, bot = TelegramLinks(world.factory), FakeBotApi()
    stack.notifications.channels.register(TelegramChannel(links, bot))
    offer = await links.create_offer(world.user)
    await links.bind(token=offer.token, chat_id=777, telegram_user_id=5)

    job, _ = await stack.service.create_job(
        user_id=world.user,
        request={"goal": "Собери сводку", "delivery": {"channels": ["push", "telegram"]}},
        origin_type="user",
        origin_id=str(world.user),
    )
    worker = world.worker(stack)
    assert await worker.tick_jobs() == 1
    await worker.wait_idle()
    assert (await stack.service.get_job(user_id=world.user, job_id=job.id)).status == "succeeded"
    assert await worker.tick_deliveries() == 2

    assert bot.sent == [(777, "Собери сводку\n\nСводка готова: три новости.")]
    async with world.factory() as session:
        rows = {
            row.channel: row.status for row in (await session.scalars(select(DeliveryRow))).all()
        }
    assert rows == {"push": "skipped", "telegram": "sent"}


@requires_postgres
async def test_unlinked_user_gets_a_skipped_telegram_delivery(world: JobsWorld) -> None:  # noqa: F811
    stack = world.stack(world.run_service(scripted(text_step(answer("Готово.")))), push=False)
    bot = FakeBotApi()
    stack.notifications.channels.register(TelegramChannel(TelegramLinks(world.factory), bot))
    await stack.service.create_job(
        user_id=world.user,
        request={"goal": "g", "delivery": {"channels": ["telegram"]}},
        origin_type="user",
        origin_id="u",
    )
    worker = world.worker(stack)
    await worker.tick_jobs()
    await worker.wait_idle()
    await worker.tick_deliveries()
    assert bot.sent == []
    async with world.factory() as session:
        [row] = (await session.scalars(select(DeliveryRow))).all()
    assert (row.channel, row.status, row.last_error) == (
        "telegram",
        "skipped",
        "channel_not_connected",
    )
