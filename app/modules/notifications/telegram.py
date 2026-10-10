"""The Mia Telegram bot (M41): deep-link binding, webhook updates and the ``telegram`` channel.

Binding: the app asks for a one-time link (``https://t.me/<bot>?start=<token>``); when the user
opens it, Telegram sends ``/start <token>`` to the webhook, which stores the private chat id for
the token's user. Only the SHA-256 of a token is stored; it lives ``TELEGRAM_LINK_TTL_SECONDS``
and works once. The bot only talks to its own user (notifications); it cannot message other
people (Review §7.5), which is why sending to a person is the L0 hand-off of the plugin.

The channel delivers M16 inbox items: ``skipped`` without a linked chat, ``retry`` for transient
errors, and a blocked bot or deleted chat unlinks the user.

Notifications are the user's choice (M77, OI-M41-002): right after linking, the bot asks
«Присылать сюда уведомления Mia?» with the inline buttons «Да» / «Нет»; the answer is saved in
the notification settings (the channel is off until then). Unlinking forgets the answer, so the
next link asks again.

Chat «Telegram» (M74, ADR-0017 §5): when the app wires runs and chats
(:class:`TelegramConversation`), a plain message from the linked user is a ``chat`` run in the
user's Telegram chat and the answer is sent back; ``/new`` keeps the current dialog as an
ordinary chat and starts a fresh one. Approvals are given in the app (the bot says so).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.runtime import Runtime
from app.modules.notifications.channels import (
    BUILTIN_CHANNELS,
    ChannelConnection,
    ChannelRegistry,
    DeliveryResult,
    OutboundMessage,
)
from app.modules.notifications.preferences import NotificationPreferences, forget_channel
from app.modules.notifications.telegram_api import (
    HttpTelegramBotApi,
    InlineButton,
    TelegramBotApi,
)
from app.modules.notifications.telegram_models import TelegramLinkRow, TelegramLinkTokenRow

if TYPE_CHECKING:
    from app.modules.conversations.chats import ChatService
    from app.modules.runs.service import RunService

logger = logging.getLogger(__name__)

TOKEN_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"
MAX_PENDING_TOKENS = 5

REPLY_LINKED = "Готово: Telegram подключён к Mia."
ASK_NOTIFY = "Присылать сюда уведомления Mia?"
ASK_YES, ASK_NO = "Да", "Нет"
NOTIFY_YES_DATA, NOTIFY_NO_DATA = "notify:telegram:yes", "notify:telegram:no"
REPLY_NOTIFY_ON = "Хорошо, уведомления Mia будут приходить сюда. Что именно — в настройках Mia."
REPLY_NOTIFY_OFF = "Хорошо, уведомления сюда приходить не будут. Включить можно в настройках Mia."
REPLY_BAD_TOKEN = (
    "Ссылка недействительна или устарела. Откройте приложение Mia и получите новую ссылку."
)
REPLY_NO_TOKEN = "Чтобы подключить Mia, откройте приложение и нажмите «Подключить Telegram»."
REPLY_UNLINKED = "Telegram отключён от Mia. Подключить снова можно в приложении."
REPLY_HELP = (
    "Я присылаю уведомления Mia: результаты фоновых задач и напоминания. "
    "Команда /stop отключает уведомления."
)
REPLY_CHAT_HELP = (
    "Пишите сюда — я отвечу, как в приложении Mia. "
    "/new — начать новый диалог, /stop — отключить Telegram."
)
REPLY_NEW_CHAT = "Начала новый диалог. Прошлый сохранён в списке чатов Mia."
REPLY_TEXT_ONLY = "Пока я понимаю здесь только текст."
REPLY_APPROVAL = "Нужно ваше подтверждение — откройте Mia в приложении или на сайте."
REPLY_FAILED = "Не получилось ответить. Попробуйте ещё раз чуть позже."
INPUT_MAX_CHARS = 16000
ANSWER_WAIT_S = 30 * 60


def utc_now() -> datetime:
    return datetime.now(UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


@dataclass(frozen=True, slots=True)
class LinkOffer:
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LinkedChat:
    user_id: uuid.UUID
    chat_id: int
    created_at: datetime
    telegram_user_id: int | None = None


class TelegramLinks:
    """Postgres store of link tokens and linked chats."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl_s: int = 900,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._ttl = timedelta(seconds=ttl_s)
        self._clock = clock

    async def create_offer(self, user_id: uuid.UUID) -> LinkOffer:
        now = self._clock()
        token = secrets.token_urlsafe(32)
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(TelegramLinkTokenRow).where(
                    TelegramLinkTokenRow.user_id == user_id,
                    (TelegramLinkTokenRow.expires_at <= now)
                    | (TelegramLinkTokenRow.used_at.is_not(None)),
                )
            )
            pending = list(
                await session.scalars(
                    select(TelegramLinkTokenRow.id)
                    .where(TelegramLinkTokenRow.user_id == user_id)
                    .order_by(TelegramLinkTokenRow.created_at.desc())
                )
            )
            if len(pending) >= MAX_PENDING_TOKENS:
                await session.execute(
                    delete(TelegramLinkTokenRow).where(
                        TelegramLinkTokenRow.id.in_(pending[MAX_PENDING_TOKENS - 1 :])
                    )
                )
            session.add(
                TelegramLinkTokenRow(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    token_hash=hash_token(token),
                    created_at=now,
                    expires_at=now + self._ttl,
                )
            )
        return LinkOffer(token=token, expires_at=now + self._ttl)

    async def bind(self, *, token: str, chat_id: int, telegram_user_id: int) -> uuid.UUID | None:
        """Consume ``token`` (once) and link the chat to its user; None when unusable."""
        now = self._clock()
        async with self._sessions() as session, session.begin():
            user_id = await session.scalar(
                update(TelegramLinkTokenRow)
                .where(
                    TelegramLinkTokenRow.token_hash == hash_token(token),
                    TelegramLinkTokenRow.used_at.is_(None),
                    TelegramLinkTokenRow.expires_at > now,
                )
                .values(used_at=now)
                .returning(TelegramLinkTokenRow.user_id)
            )
            if user_id is None:
                return None
            await session.execute(
                delete(TelegramLinkRow).where(
                    (TelegramLinkRow.chat_id == chat_id) & (TelegramLinkRow.user_id != user_id)
                )
            )
            statement = insert(TelegramLinkRow).values(
                id=uuid.uuid4(),
                user_id=user_id,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                created_at=now,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_telegram_links_user",
                    set_={
                        "chat_id": chat_id,
                        "telegram_user_id": telegram_user_id,
                        "created_at": now,
                    },
                )
            )
        return user_id

    async def chat_for(self, user_id: uuid.UUID) -> LinkedChat | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(TelegramLinkRow).where(TelegramLinkRow.user_id == user_id)
            )
        if row is None:
            return None
        return LinkedChat(user_id=row.user_id, chat_id=row.chat_id, created_at=row.created_at)

    async def user_for_chat(self, chat_id: int) -> LinkedChat | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(TelegramLinkRow).where(TelegramLinkRow.chat_id == chat_id)
            )
        if row is None:
            return None
        return LinkedChat(
            user_id=row.user_id,
            chat_id=row.chat_id,
            created_at=row.created_at,
            telegram_user_id=row.telegram_user_id,
        )

    async def unlink_user(self, user_id: uuid.UUID) -> bool:
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                delete(TelegramLinkRow).where(TelegramLinkRow.user_id == user_id)
            )
            await forget_channel(session, user_id, "telegram")
        return bool(result.rowcount)

    async def unlink_chat(self, chat_id: int) -> bool:
        async with self._sessions() as session, session.begin():
            owner = await session.scalar(
                delete(TelegramLinkRow)
                .where(TelegramLinkRow.chat_id == chat_id)
                .returning(TelegramLinkRow.user_id)
            )
            if owner is not None:
                await forget_channel(session, owner, "telegram")
        return owner is not None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def answer_text(response: Mapping[str, Any] | None) -> str:
    """The text a Telegram user gets for a run's answer: display markdown, else speech."""
    if not response:
        return ""
    display = response.get("display")
    if isinstance(display, Mapping):
        markdown = display.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown
        if display.get("kind") == "link" and isinstance(display.get("url"), str):
            return f"{response.get('speech') or ''}\n\n{display['url']}".strip()
    speech = response.get("speech")
    return speech if isinstance(speech, str) else ""


class TelegramConversation:
    """Messages of a linked user become runs in their Telegram chat; answers go back."""

    def __init__(
        self,
        runs: RunService,
        chats: ChatService,
        api: TelegramBotApi,
        *,
        poll_s: float = 1.0,
        wait_s: float = ANSWER_WAIT_S,
    ) -> None:
        self._runs = runs
        self._chats = chats
        self._api = api
        self._poll_s = poll_s
        self._wait_s = wait_s
        self._tasks: set[asyncio.Task[None]] = set()

    async def new_chat(self, linked: LinkedChat) -> None:
        await self._chats.start_new_telegram_chat(user_id=linked.user_id)
        await self._api.send_message(linked.chat_id, REPLY_NEW_CHAT)

    async def ask(self, linked: LinkedChat, text: str, update_id: int | None) -> uuid.UUID | None:
        """Start the run; the answer is sent by a background task. None for a replayed update."""
        from app.modules.runs.domain import NewRun

        conversation_id = await self._chats.telegram_chat_id(user_id=linked.user_id)
        record, replayed = await self._runs.start(
            NewRun(
                user_id=linked.user_id,
                profile="chat",
                input_text=text[:INPUT_MAX_CHARS],
                conversation_id=conversation_id,
            ),
            idempotency_key=None if update_id is None else f"telegram-update-{update_id}",
            request_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        if replayed:
            return None
        task = asyncio.create_task(self._reply(linked, record.id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record.id

    async def _reply(self, linked: LinkedChat, run_id: uuid.UUID) -> None:
        deadline = time.monotonic() + self._wait_s
        asked_approval = False
        try:
            while True:
                if self._runs.is_live(run_id):
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self._runs.wait_finished(run_id)),
                            timeout=self._poll_s,
                        )
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(self._poll_s)
                record = await self._runs.get(user_id=linked.user_id, run_id=run_id)
                if record.terminal:
                    break
                if record.status == "waiting_approval" and not asked_approval:
                    asked_approval = True
                    await self._api.send_message(linked.chat_id, REPLY_APPROVAL)
                if time.monotonic() > deadline:
                    return
            text = answer_text(record.response) if record.status == "completed" else ""
            await self._api.send_message(linked.chat_id, text.strip() or REPLY_FAILED)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("telegram_answer_failed")

    async def wait_idle(self) -> None:
        """Wait until every pending answer is sent (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)


class TelegramBot:
    """Handles webhook updates; never raises for a malformed update."""

    def __init__(
        self,
        links: TelegramLinks,
        api: TelegramBotApi,
        conversation: TelegramConversation | None = None,
        preferences: NotificationPreferences | None = None,
    ) -> None:
        self._links = links
        self._api = api
        self.conversation = conversation
        self.preferences = preferences

    async def handle_update(self, update_body: Mapping[str, Any]) -> None:
        callback = update_body.get("callback_query")
        if isinstance(callback, Mapping):
            await self._answer(callback)
            return
        message = update_body.get("message")
        if not isinstance(message, Mapping):
            return
        chat, sender = message.get("chat"), message.get("from")
        if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
            return
        chat_id, sender_id = _int(chat.get("id")), _int(sender.get("id"))
        if chat_id is None or sender_id is None or chat.get("type") != "private":
            return
        if sender.get("is_bot") is True:
            return
        text = message.get("text")
        command, _, argument = (text if isinstance(text, str) else "").strip().partition(" ")
        command = command.split("@", 1)[0].lower()
        if command == "/start":
            await self._start(chat_id, sender_id, argument.strip())
        elif command == "/stop":
            await self._links.unlink_chat(chat_id)
            await self._api.send_message(chat_id, REPLY_UNLINKED)
        elif self.conversation is None:
            await self._api.send_message(chat_id, REPLY_HELP)
        else:
            await self._converse(update_body, chat_id, sender_id, command, text)

    async def _converse(
        self,
        update_body: Mapping[str, Any],
        chat_id: int,
        sender_id: int,
        command: str,
        text: object,
    ) -> None:
        conversation = self.conversation
        assert conversation is not None
        linked = await self._links.user_for_chat(chat_id)
        if linked is None or linked.telegram_user_id not in (None, sender_id):
            await self._api.send_message(chat_id, REPLY_NO_TOKEN)
        elif command == "/new":
            await conversation.new_chat(linked)
        elif command.startswith("/"):
            await self._api.send_message(chat_id, REPLY_CHAT_HELP)
        elif not isinstance(text, str) or not text.strip():
            await self._api.send_message(chat_id, REPLY_TEXT_ONLY)
        else:
            await conversation.ask(linked, text.strip(), _int(update_body.get("update_id")))

    async def _start(self, chat_id: int, sender_id: int, token: str) -> None:
        if not token:
            await self._api.send_message(chat_id, REPLY_NO_TOKEN)
            return
        bound = (
            await self._links.bind(token=token, chat_id=chat_id, telegram_user_id=sender_id)
            if re.fullmatch(TOKEN_PATTERN, token)
            else None
        )
        await self._api.send_message(chat_id, REPLY_LINKED if bound else REPLY_BAD_TOKEN)
        if bound is not None:
            await self._ask_notifications(chat_id, bound)

    async def _ask_notifications(self, chat_id: int, user_id: uuid.UUID) -> None:
        """A newly connected channel asks before it is used (M77)."""
        preferences = self.preferences
        if preferences is None or await preferences.decided(user_id, "telegram"):
            return
        await self._api.send_message(
            chat_id,
            ASK_NOTIFY,
            [InlineButton(ASK_YES, NOTIFY_YES_DATA), InlineButton(ASK_NO, NOTIFY_NO_DATA)],
        )

    async def _answer(self, callback: Mapping[str, Any]) -> None:
        """«Да» / «Нет» under the question: only the linked Telegram user may answer."""
        callback_id, data = callback.get("id"), callback.get("data")
        if not isinstance(callback_id, str):
            return
        await self._api.answer_callback(callback_id)
        sender, message = callback.get("from"), callback.get("message")
        if data not in (NOTIFY_YES_DATA, NOTIFY_NO_DATA) or self.preferences is None:
            return
        if not isinstance(sender, Mapping) or not isinstance(message, Mapping):
            return
        chat = message.get("chat")
        chat_id = _int(chat.get("id")) if isinstance(chat, Mapping) else None
        message_id, sender_id = _int(message.get("message_id")), _int(sender.get("id"))
        if chat_id is None or message_id is None or sender_id is None:
            return
        linked = await self._links.user_for_chat(chat_id)
        if linked is None or linked.telegram_user_id not in (None, sender_id):
            return
        enabled = data == NOTIFY_YES_DATA
        await self.preferences.decide(linked.user_id, "telegram", enabled)
        await self._api.edit_message(
            chat_id, message_id, REPLY_NOTIFY_ON if enabled else REPLY_NOTIFY_OFF
        )


class TelegramChannel:
    """``NotificationChannel`` named ``telegram``: inbox items to the user's linked chat."""

    name = "telegram"
    label = BUILTIN_CHANNELS["telegram"]

    def __init__(self, links: TelegramLinks, api: TelegramBotApi) -> None:
        self._links = links
        self._api = api

    async def connection(self, user_id: uuid.UUID) -> ChannelConnection:
        if await self._links.chat_for(user_id) is None:
            return ChannelConnection(False, "not_linked")
        return ChannelConnection(True)

    @staticmethod
    def render(message: OutboundMessage) -> str:
        title, text = message.title.strip(), message.text.strip()
        if text and title and text != title:
            return f"{title}\n\n{text}"
        return text or title

    async def deliver(self, message: OutboundMessage) -> DeliveryResult:
        chat = await self._links.chat_for(message.user_id)
        if chat is None:
            return DeliveryResult("skipped", "channel_not_connected")
        result = await self._api.send_message(chat.chat_id, self.render(message))
        if result.outcome == "sent":
            return DeliveryResult("sent")
        if result.outcome == "blocked":
            await self._links.unlink_chat(chat.chat_id)
            return DeliveryResult("failed", result.error_code)
        if result.outcome == "retry":
            return DeliveryResult("retry", result.error_code)
        return DeliveryResult("failed", result.error_code)


@dataclass(frozen=True, slots=True)
class TelegramStack:
    links: TelegramLinks
    api: TelegramBotApi | None
    bot: TelegramBot | None
    channel: TelegramChannel | None
    bot_username: str | None
    webhook_secret: str | None

    @property
    def configured(self) -> bool:
        return self.bot is not None and self.bot_username is not None

    def deep_link(self, token: str) -> str | None:
        if self.bot_username is None:
            return None
        return f"https://t.me/{self.bot_username}?start={token}"


def build_telegram(
    settings: Settings,
    runtime: Runtime,
    *,
    api: TelegramBotApi | None = None,
    preferences: NotificationPreferences | None = None,
) -> TelegramStack:
    links = TelegramLinks(
        runtime.database.session_factory, ttl_s=settings.telegram_link_ttl_seconds
    )
    token = settings.telegram_bot_token
    if api is None and token is not None:
        api = HttpTelegramBotApi(
            token.get_secret_value(),
            base_url=settings.telegram_api_base,
            timeout_s=settings.telegram_request_timeout_seconds,
        )
    secret = settings.telegram_webhook_secret
    username = settings.telegram_bot_username
    return TelegramStack(
        links=links,
        api=api,
        bot=TelegramBot(links, api, preferences=preferences) if api is not None else None,
        channel=TelegramChannel(links, api) if api is not None else None,
        bot_username=username.removeprefix("@") if username else None,
        webhook_secret=secret.get_secret_value() if secret is not None else None,
    )


def install_telegram(
    app: FastAPI,
    settings: Settings,
    runtime: Runtime,
    *,
    channels: ChannelRegistry,
    api: TelegramBotApi | None = None,
    preferences: NotificationPreferences | None = None,
) -> TelegramStack:
    """Mount ``/v1/telegram*``, register the ``telegram`` channel; ``app.state.telegram``.

    ``preferences`` (the notification settings) lets the bot ask «Присылать сюда уведомления
    Mia?» when a chat is linked.
    """
    from app.api.v1.telegram import build_telegram_router
    from app.modules.auth.composition import request_authenticator

    stack = build_telegram(settings, runtime, api=api, preferences=preferences)
    if stack.channel is not None:
        channels.register(stack.channel)
    chats = getattr(app.state, "chats", None)
    runs = getattr(app.state, "runs", None)
    if stack.bot is not None and stack.api is not None and chats is not None and runs is not None:
        stack.bot.conversation = TelegramConversation(runs, chats.chats, stack.api)
    app.state.telegram = stack
    app.include_router(build_telegram_router(stack=stack, authenticator=request_authenticator(app)))
    return stack
