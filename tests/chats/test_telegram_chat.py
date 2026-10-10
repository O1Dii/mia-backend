"""Chat «Telegram» (ADR-0017 §5): the bot dialog is a chat with its own context; ``/new``."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.ports.model_gateway import ModelGatewayError
from app.modules.conversations.models import Conversation
from app.modules.notifications.channels import ChannelRegistry
from app.modules.notifications.telegram import (
    REPLY_CHAT_HELP,
    REPLY_FAILED,
    REPLY_NEW_CHAT,
    REPLY_NO_TOKEN,
    REPLY_TEXT_ONLY,
    TelegramConversation,
    TelegramStack,
    answer_text,
    install_telegram,
)
from sqlalchemy import select

from tests.chats.conftest import ChatWorld, _Runtime, check
from tests.plugins.telegram.conftest import FakeBotApi, telegram_settings
from tests.workspace.harness import requires_postgres

pytestmark = requires_postgres
CHAT = 4242
SENDER = 777


def update(
    text_value: Any, *, update_id: int, chat: int = CHAT, sender: int = SENDER
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "chat": {"id": chat, "type": "private"},
        "from": {"id": sender, "is_bot": False},
    }
    if text_value is not None:
        message["text"] = text_value
    return {"update_id": update_id, "message": message}


async def linked_bot(world: ChatWorld, user: uuid.UUID) -> tuple[TelegramStack, FakeBotApi]:
    api = FakeBotApi()
    stack = install_telegram(
        world.harness.app,
        telegram_settings(),
        _Runtime(world.factory),  # type: ignore[arg-type]
        channels=ChannelRegistry(),
        api=api,
    )
    assert stack.bot is not None
    assert isinstance(stack.bot.conversation, TelegramConversation)
    stack.bot.conversation._poll_s = 0.05  # noqa: SLF001 - fast polling in tests
    offer = await stack.links.create_offer(user)
    await stack.bot.handle_update(update(f"/start {offer.token}", update_id=1))
    api.sent.clear()
    return stack, api


async def say(stack: TelegramStack, text_value: Any, update_id: int, **kwargs: Any) -> None:
    assert stack.bot is not None and stack.bot.conversation is not None
    await stack.bot.handle_update(update(text_value, update_id=update_id, **kwargs))
    await stack.bot.conversation.wait_idle()


async def telegram_chats(world: ChatWorld, user: uuid.UUID) -> list[Conversation]:
    async with world.factory() as session:
        return list(
            (
                await session.scalars(
                    select(Conversation)
                    .where(Conversation.user_id == user)
                    .order_by(Conversation.created_at)
                )
            ).all()
        )


async def test_bot_dialog_is_the_telegram_chat(world: ChatWorld) -> None:
    world.harness.app.state.runs = world.runs
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        stack, api = await linked_bot(world, user)

        await say(stack, "Привет, это Telegram", 10)
        await say(stack, "Что я сказал?", 11)
        assert api.sent == [(CHAT, "Ответ 1."), (CHAT, "Ответ 2.")]
        history = [m.content for m in world.gateway.requests[-1].messages[1:-1]]
        assert history == ["Привет, это Telegram", "Ответ 1."]

        listing = (await client.get("/v1/chats", headers=headers)).json()
        check("ChatPage", listing)
        [chat] = listing["items"]
        assert (chat["kind"], chat["title"]) == ("telegram", "Telegram")

        # A retried webhook update starts nothing new.
        await say(stack, "Что я сказал?", 11)
        assert len(api.sent) == 2

        await say(stack, "/new", 12)
        assert api.sent[-1] == (CHAT, REPLY_NEW_CHAT)
        await say(stack, "Новая тема", 13)
        assert list(world.gateway.requests[-1].messages[1:-1]) == []
        rows = await telegram_chats(world, user)
        assert [row.kind for row in rows] == ["chat", "telegram"]
        assert rows[0].title is not None and rows[0].title.startswith("Telegram · ")

        # The web sees both; the old Telegram dialog is an ordinary chat now.
        kinds = {
            item["kind"]
            for item in (await client.get("/v1/chats", headers=headers)).json()["items"]
        }
        assert kinds == {"chat", "telegram"}


async def test_unlinked_senders_commands_and_failures(world: ChatWorld) -> None:
    world.harness.app.state.runs = world.runs
    async with world.harness.client() as client:
        _, user = await world.login(client, "alice")
        stack, api = await linked_bot(world, user)

        await say(stack, "Привет", 20, chat=999, sender=999)
        await say(stack, "Чужой отправитель", 21, sender=555)
        await say(stack, "/help", 22)
        await say(stack, None, 23)
        assert [text for _, text in api.sent] == [
            REPLY_NO_TOKEN,
            REPLY_NO_TOKEN,
            REPLY_CHAT_HELP,
            REPLY_TEXT_ONLY,
        ]
        assert world.gateway.requests == []

        original = world.gateway.stream

        async def broken(request: Any) -> Any:
            raise ModelGatewayError("model_not_configured", "no model")
            yield  # pragma: no cover

        world.gateway.stream = broken  # type: ignore[method-assign]
        await say(stack, "Ответь", 24)
        world.gateway.stream = original  # type: ignore[method-assign]
        assert api.sent[-1] == (CHAT, REPLY_FAILED)


def test_answer_text() -> None:
    assert answer_text(None) == ""
    assert answer_text({"speech": "Да.", "display": None}) == "Да."
    assert (
        answer_text({"speech": "Да.", "display": {"kind": "text", "markdown": "**Да**"}})
        == "**Да**"
    )
    link = {"speech": "Вот.", "display": {"kind": "link", "url": "https://a.test", "title": "A"}}
    assert answer_text(link) == "Вот.\n\nhttps://a.test"
