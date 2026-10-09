"""The Mia Telegram bot (M41): deep-link binding, webhook updates and the ``telegram`` channel.

Binding: the app asks for a one-time link (``https://t.me/<bot>?start=<token>``); when the user
opens it, Telegram sends ``/start <token>`` to the webhook, which stores the private chat id for
the token's user. Only the SHA-256 of a token is stored; it lives ``TELEGRAM_LINK_TTL_SECONDS``
and works once. The bot only talks to its own user (notifications); it cannot message other
people (Review §7.5), which is why sending to a person is the L0 hand-off of the plugin.

The channel delivers M16 inbox items: ``skipped`` without a linked chat, ``retry`` for transient
errors, and a blocked bot or deleted chat unlinks the user.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.runtime import Runtime
from app.modules.notifications.channels import ChannelRegistry, DeliveryResult, OutboundMessage
from app.modules.notifications.telegram_api import HttpTelegramBotApi, TelegramBotApi
from app.modules.notifications.telegram_models import TelegramLinkRow, TelegramLinkTokenRow

logger = logging.getLogger(__name__)

TOKEN_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"
MAX_PENDING_TOKENS = 5

REPLY_LINKED = "Готово: Telegram подключён к Mia. Сюда будут приходить результаты фоновых задач."
REPLY_BAD_TOKEN = (
    "Ссылка недействительна или устарела. Откройте приложение Mia и получите новую ссылку."
)
REPLY_NO_TOKEN = "Чтобы подключить Mia, откройте приложение и нажмите «Подключить Telegram»."
REPLY_UNLINKED = "Telegram отключён от Mia. Подключить снова можно в приложении."
REPLY_HELP = (
    "Я присылаю уведомления Mia: результаты фоновых задач и напоминания. "
    "Команда /stop отключает уведомления."
)


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

    async def unlink_user(self, user_id: uuid.UUID) -> bool:
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                delete(TelegramLinkRow).where(TelegramLinkRow.user_id == user_id)
            )
        return bool(result.rowcount)

    async def unlink_chat(self, chat_id: int) -> bool:
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                delete(TelegramLinkRow).where(TelegramLinkRow.chat_id == chat_id)
            )
        return bool(result.rowcount)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class TelegramBot:
    """Handles webhook updates; never raises for a malformed update."""

    def __init__(self, links: TelegramLinks, api: TelegramBotApi) -> None:
        self._links = links
        self._api = api

    async def handle_update(self, update_body: Mapping[str, Any]) -> None:
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
        else:
            await self._api.send_message(chat_id, REPLY_HELP)

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


class TelegramChannel:
    """``NotificationChannel`` named ``telegram``: inbox items to the user's linked chat."""

    name = "telegram"

    def __init__(self, links: TelegramLinks, api: TelegramBotApi) -> None:
        self._links = links
        self._api = api

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
    settings: Settings, runtime: Runtime, *, api: TelegramBotApi | None = None
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
        bot=TelegramBot(links, api) if api is not None else None,
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
) -> TelegramStack:
    """Mount ``/v1/telegram*``, register the ``telegram`` channel; ``app.state.telegram``."""
    from app.api.v1.telegram import build_telegram_router
    from app.modules.auth.composition import request_authenticator

    stack = build_telegram(settings, runtime, api=api)
    if stack.channel is not None:
        channels.register(stack.channel)
    app.state.telegram = stack
    app.include_router(build_telegram_router(stack=stack, authenticator=request_authenticator(app)))
    return stack
