"""M77: a newly linked Telegram bot asks «Присылать сюда уведомления Mia?» and saves the answer;
a timer of a user who said «Да» arrives in the bot (fake Bot API)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.modules.jobs.composition import JobsStack
from app.modules.notifications.models import NotificationChannelSettingRow
from app.modules.notifications.preferences import NotificationPreferences
from app.modules.notifications.telegram import (
    ASK_NOTIFY,
    NOTIFY_NO_DATA,
    NOTIFY_YES_DATA,
    REPLY_LINKED,
    REPLY_NO_TOKEN,
    REPLY_NOTIFY_OFF,
    REPLY_NOTIFY_ON,
    TelegramStack,
    build_telegram,
)
from sqlalchemy import select

from tests.jobs.conftest import JobsWorld, _Runtime, requires_postgres
from tests.jobs.test_schedules import _item, _plugin_state
from tests.plugins.telegram.conftest import FakeBotApi, telegram_settings
from tests.runs.gateways import scripted

pytestmark = requires_postgres
CHAT = 4242
SENDER = 777


def start(
    token: str, *, update_id: int = 1, chat: int = CHAT, sender: int = SENDER
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat, "type": "private"},
            "from": {"id": sender, "is_bot": False},
            "text": f"/start {token}",
        },
    }


def press(
    data: str, *, sender: int = SENDER, chat: int = CHAT, callback_id: str = "cb1"
) -> dict[str, Any]:
    return {
        "update_id": 99,
        "callback_query": {
            "id": callback_id,
            "from": {"id": sender, "is_bot": False},
            "message": {"message_id": 55, "chat": {"id": chat, "type": "private"}},
            "data": data,
        },
    }


class Bot:
    """The real bot, channel and links on the jobs stack, with a fake Bot API."""

    def __init__(self, world: JobsWorld) -> None:
        self.world = world
        self.api = FakeBotApi()
        self.jobs: JobsStack = world.stack(world.run_service(scripted()), push=False)
        self.telegram: TelegramStack = build_telegram(
            telegram_settings(),
            _Runtime(world.factory),  # type: ignore[arg-type]
            api=self.api,
            preferences=self.jobs.notifications.preferences,
        )
        assert self.telegram.channel is not None and self.telegram.bot is not None
        self.jobs.notifications.channels.register(self.telegram.channel)

    @property
    def preferences(self) -> NotificationPreferences:
        return self.jobs.notifications.preferences

    async def link(self, user: uuid.UUID | None = None) -> None:
        offer = await self.telegram.links.create_offer(user or self.world.user)
        assert self.telegram.bot is not None
        await self.telegram.bot.handle_update(start(offer.token))

    async def answer(self, data: str, **kwargs: Any) -> None:
        assert self.telegram.bot is not None
        await self.telegram.bot.handle_update(press(data, **kwargs))

    async def telegram_status(self) -> dict[str, Any]:
        snapshot = await self.preferences.snapshot(self.world.user)
        return next(c for c in snapshot["channels"] if c["name"] == "telegram")


async def test_linking_asks_and_yes_enables_the_channel(world: JobsWorld) -> None:
    bot = Bot(world)
    before = await bot.telegram_status()
    assert (before["status"], before["detail"], before["enabled"]) == (
        "not_connected",
        "not_linked",
        False,
    )

    await bot.link()
    assert bot.api.sent[0] == (CHAT, REPLY_LINKED)
    [(chat, text, buttons)] = bot.api.buttons
    assert (chat, text) == (CHAT, ASK_NOTIFY)
    assert [(b.text, b.data) for b in buttons] == [("Да", NOTIFY_YES_DATA), ("Нет", NOTIFY_NO_DATA)]
    # Linked, but nothing is sent here until the user answers.
    status = await bot.telegram_status()
    assert (status["status"], status["enabled"], status["decided"]) == ("connected", False, False)
    assert "telegram" not in await bot.preferences.route(world.user, "job_result")

    await bot.answer(NOTIFY_YES_DATA)
    assert bot.api.answered == ["cb1"]
    assert bot.api.edited == [(CHAT, 55, REPLY_NOTIFY_ON)]
    status = await bot.telegram_status()
    assert (status["status"], status["enabled"], status["decided"]) == ("connected", True, True)
    assert await bot.preferences.route(world.user, "job_result") == ("push", "telegram")


async def test_no_keeps_the_channel_off_and_unlinking_makes_the_next_link_ask_again(
    world: JobsWorld,
) -> None:
    bot = Bot(world)
    await bot.link()
    await bot.answer(NOTIFY_NO_DATA)
    assert bot.api.edited == [(CHAT, 55, REPLY_NOTIFY_OFF)]
    status = await bot.telegram_status()
    assert (status["enabled"], status["decided"]) == (False, True)
    assert await bot.preferences.route(world.user, "timer_alarm") == ("push",)

    # «/stop» disconnects and forgets the answer; linking again asks again.
    assert bot.telegram.bot is not None
    await bot.telegram.bot.handle_update(
        {
            "update_id": 5,
            "message": {
                "chat": {"id": CHAT, "type": "private"},
                "from": {"id": SENDER, "is_bot": False},
                "text": "/stop",
            },
        }
    )
    status = await bot.telegram_status()
    assert (status["status"], status["decided"]) == ("not_connected", False)
    bot.api.buttons.clear()
    await bot.link()
    assert len(bot.api.buttons) == 1

    # Unlinking from the app (DELETE /v1/telegram/link) forgets it as well.
    await bot.answer(NOTIFY_YES_DATA)
    assert (await bot.telegram_status())["enabled"] is True
    await bot.telegram.links.unlink_user(world.user)
    assert (await bot.telegram_status())["decided"] is False


async def test_only_the_linked_telegram_user_can_answer(world: JobsWorld) -> None:
    bot = Bot(world)
    await bot.answer(NOTIFY_YES_DATA)  # nobody is linked in this chat
    await bot.link()
    await bot.answer(NOTIFY_YES_DATA, sender=SENDER + 1)  # someone else in the chat
    await bot.answer("notify:telegram:maybe")  # not ours
    await bot.answer("something:else")
    assert bot.api.edited == []
    assert len(bot.api.answered) == 4  # every press stops its spinner
    async with world.factory() as session:
        assert (await session.scalars(select(NotificationChannelSettingRow))).all() == []


async def test_a_bad_link_does_not_ask(world: JobsWorld) -> None:
    bot = Bot(world)
    assert bot.telegram.bot is not None
    await bot.telegram.bot.handle_update(start("x" * 20))
    assert bot.api.buttons == []


async def test_a_timer_reaches_the_bot_once_the_user_said_yes(world: JobsWorld) -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    world.clock.set(now)
    bot = Bot(world)
    await bot.link()
    await _plugin_state(world, "builtin.timer", [_item("t1", "Паста", now - timedelta(seconds=3))])

    # Not answered yet: the timer is stored in the inbox, nothing is sent to Telegram.
    worker = world.worker(bot.jobs)
    assert await worker.tick_deadlines() == 1
    await bot.jobs.notifications.dispatcher.run_once()
    assert [text for chat, text in bot.api.sent if chat == CHAT and "Паста" in text] == []

    await bot.answer(NOTIFY_YES_DATA)
    await _plugin_state_more(world, now)
    assert await worker.tick_deadlines() == 1
    await bot.jobs.notifications.dispatcher.run_once()
    sent = [text for chat, text in bot.api.sent if chat == CHAT and "Чай" in text]
    assert sent == ["Таймер: Чай\n\nТаймер «Чай» сработал."]


async def _plugin_state_more(world: JobsWorld, now: datetime) -> None:
    """A second timer of the same user fires after the answer."""
    from app.modules.workspace.models import PluginState
    from app.modules.workspace.service import ensure_personal_workspace

    async with world.factory() as session, session.begin():
        workspace_id = await ensure_personal_workspace(session, world.user)
        state = await session.get(PluginState, (workspace_id, "builtin.timer"))
        assert state is not None
        state.data = {
            "items": [*state.data["items"], _item("t2", "Чай", now - timedelta(seconds=1))]
        }


async def test_start_again_asks_a_user_who_linked_before_the_question_existed(
    world: JobsWorld,
) -> None:
    bot = Bot(world)
    # Linked by the old flow: a link row, no stored answer.
    offer = await bot.telegram.links.create_offer(world.user)
    await bot.telegram.links.bind(token=offer.token, chat_id=CHAT, telegram_user_id=SENDER)
    assert bot.api.sent == []
    assert (await bot.telegram_status())["decided"] is False

    assert bot.telegram.bot is not None
    plain_start = start("").copy()
    plain_start["message"]["text"] = "/start"
    await bot.telegram.bot.handle_update(plain_start)
    [(chat, text, buttons)] = bot.api.buttons
    assert (chat, text, len(buttons)) == (CHAT, ASK_NOTIFY, 2)

    await bot.answer(NOTIFY_YES_DATA)
    bot.api.sent.clear()
    await bot.telegram.bot.handle_update(plain_start)  # answered: a short confirmation only
    assert bot.api.sent == [(CHAT, REPLY_LINKED)]

    # Somebody else's chat is told how to link.
    bot.api.sent.clear()
    await bot.telegram.bot.handle_update(
        {**plain_start, "message": {**plain_start["message"], "chat": {"id": 5, "type": "private"}}}
    )
    assert [text for _, text in bot.api.sent] == [REPLY_NO_TOKEN]
