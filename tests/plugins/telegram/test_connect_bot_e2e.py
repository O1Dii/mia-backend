"""M41-R1 DoD through the run engine (Postgres run store and feed, real router, real Telegram
plugin and bot stack): «подключи Telegram-бота» puts a button with the one-time link on the
user's screen (``tool.call.completed.action``, feed after reload until expiry) while the
model never sees the link, neither in the tool result nor in the next run's history."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.api.v1.feed import feed_item_body
from app.modules.conversations.feed import FeedService
from app.modules.runs import EngineConfig, RunEngine, RunNotifier, RunService, SqlRunStore

from tests.fakes import FakeModelGateway, text_step, tool_step
from tests.fakes.contracts import assert_mvp_valid
from tests.plugins.telegram.conftest import PLUGIN_ID, TelegramWorld, requires_postgres
from tests.runs.conftest import TEST_LIMITS, assert_well_formed, collect, types
from tests.runs.gateways import answer, scripted

pytestmark = requires_postgres

CONNECT = f"{PLUGIN_ID}.connect_bot"
LINK = re.compile(r"^https://t\.me/mia_test_bot\?start=([A-Za-z0-9_-]{43})$")


def service_for(tg: TelegramWorld, gateway: FakeModelGateway) -> RunService:
    engine = RunEngine(
        store=SqlRunStore(tg.factory),
        notifier=RunNotifier(),
        gateway=gateway,
        config=EngineConfig(limits=TEST_LIMITS),
        router=tg.router,
        approvals=tg.approvals,
        memory=tg.service,
        feed=FeedService(tg.factory),
    )
    return RunService(engine, keepalive_s=0.2)


def model_text(gateway: FakeModelGateway) -> str:
    """Everything the model was sent: instructions, history, tool results."""
    return "\n".join(m.content or "" for r in gateway.requests for m in r.messages)


async def run_once(tg: TelegramWorld, gateway: FakeModelGateway, text: str) -> list[dict[str, Any]]:
    service = service_for(tg, gateway)
    record, _ = await service.start(tg.new_run(text, device=False))
    events = await collect(service, tg.user, record.id)
    await service.wait_finished(record.id)
    assert_well_formed(events)
    await service.aclose()
    return events


async def test_connect_bot_shows_a_button_and_keeps_the_link_from_the_model(
    tg: TelegramWorld,
) -> None:
    gateway = scripted(
        tool_step("c1", CONNECT, {}),
        text_step(answer("Нажми кнопку «Открыть Telegram», затем Start в боте.")),
    )

    events = await run_once(tg, gateway, "подключи telegram бота")

    assert types(events) == [
        "run.started",
        "tool.call.started",
        "tool.call.completed",
        "response.final",
        "run.completed",
    ]
    done = next(e["payload"] for e in events if e["type"] == "tool.call.completed")
    assert done["status"] == "succeeded"
    action = done["action"]
    match = LINK.match(action["url"])
    assert match, action
    token = match.group(1)
    assert action["kind"] == "open_url" and action["label"] == "Открыть Telegram"
    expires = datetime.fromisoformat(action["expires_at"])
    assert datetime.now(UTC) < expires <= datetime.now(UTC) + timedelta(minutes=16)
    assert "deep_link" not in done["structured_content"]
    # The model saw the tool's text only: no link, no token.
    seen = model_text(gateway)
    assert "Открыть Telegram" in seen
    assert token not in seen and "t.me" not in seen
    assert token not in str(next(e for e in events if e["type"] == "response.final"))

    # After a reload the feed still carries the button with the answer…
    page = await FeedService(tg.factory).history(user_id=tg.user)
    reply = page.items[0]
    assert reply.role == "assistant" and reply.actions == (action,)
    body = feed_item_body(reply)
    assert body["actions"] == [action]
    assert_mvp_valid(body["actions"][0], "common", "screenAction")
    assert "actions" not in feed_item_body(page.items[1])  # the user's message

    # …until it expires: then the URL is gone and clients render it disabled.
    later = FeedService(tg.factory, clock=lambda: expires + timedelta(seconds=1))
    [expired] = (await later.history(user_id=tg.user)).items[0].actions
    assert expired == {
        "kind": "open_url",
        "label": "Открыть Telegram",
        "expires_at": action["expires_at"],
        "expired": True,
    }

    # The next run's model history holds the answer text, never the link.
    follow_up = scripted(text_step(answer("Да, жду, когда нажмёшь Start.")))
    await run_once(tg, follow_up, "ну что, подключился?")
    history = model_text(follow_up)
    assert "Нажми кнопку «Открыть Telegram»" in history
    assert token not in history and "t.me" not in history


async def test_an_already_linked_bot_shows_no_button(tg: TelegramWorld) -> None:
    offer = await tg.stack.links.create_offer(tg.user)
    assert await tg.stack.links.bind(token=offer.token, chat_id=777, telegram_user_id=1)
    gateway = scripted(tool_step("c1", CONNECT, {}), text_step(answer("Бот уже подключён.")))

    events = await run_once(tg, gateway, "подключи telegram бота")

    done = next(e["payload"] for e in events if e["type"] == "tool.call.completed")
    assert done["status"] == "succeeded" and "action" not in done
    assert done["structured_content"] == {"linked": True}
    page = await FeedService(tg.factory).history(user_id=tg.user)
    assert page.items[0].actions == ()
