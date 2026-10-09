"""Production :class:`TelegramHost`: people from Mia, the run's phone, the bot link.

- ``person:<id>`` and names resolve through the Memory Ledger (M17); a name nobody in memory
  carries is searched in the phone's contacts (``device.contacts.search``) and gets a
  ``contact:<n>`` ref in the per-run contacts cache, as ``people.resolve`` does.
- The hand-off is the device tool ``telegram.handoff`` (M23) on the device that started the run.
  Numbers and ids never leave the core except as the contact's display name, which the phone
  resolves locally, or a Telegram username stored for the person.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.device_tool_channel import DeviceToolCall, DeviceToolChannel, DeviceToolResult
from app.modules.memory.service import MemoryService
from app.modules.memory.tools import CONTACTS_TOOL, ContactsCache, parse_contacts
from app.modules.notifications.telegram import TelegramStack
from app.modules.plugins.builtin.identity import CallIdentity
from app.modules.runs.models import RunRow
from app.plugins.builtin.telegram.host import (
    BotLinkOffer,
    HandoffOutcome,
    HostUnavailableError,
    Recipient,
)

HANDOFF_TOOL = "telegram.handoff"
PERSON_REF = re.compile(r"^person:([0-9a-fA-F-]{36})$")
CONTACT_REF = re.compile(r"^contact:[0-9]{1,3}$")
NO_PHONE = "Для отправки в Telegram нужен телефон с приложением Mia: этот запрос пришёл без него."

DEVICE_ERRORS = {
    "unsupported_tool": "В приложении на телефоне нет передачи в Telegram: его нужно обновить.",
    "os_permission_denied": "Нет доступа к контактам телефона. Разрешите его и повторите.",
    "device_locked": "Телефон заблокирован: разблокируйте его и повторите.",
    "user_cancelled": "Вы отменили действие на телефоне. Ничего не отправлено.",
}


class MiaTelegramHost:
    def __init__(
        self,
        *,
        memory: MemoryService,
        contacts: ContactsCache,
        channel: DeviceToolChannel,
        session_factory: async_sessionmaker[AsyncSession],
        telegram: TelegramStack,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._memory = memory
        self._contacts = contacts
        self._channel = channel
        self._sessions = session_factory
        self._telegram = telegram
        self._clock = clock

    # -- people ---------------------------------------------------------------------------

    async def candidates(self, identity: CallIdentity, to: str) -> list[Recipient]:
        user = identity.user_id
        person_ref = PERSON_REF.match(to)
        if person_ref is not None:
            return await self._person_by_id(user, person_ref.group(1))
        if CONTACT_REF.match(to):
            if identity.run_id is None:
                return []
            contact = self._contacts.get(user_id=user, run_id=identity.run_id, ref=to)
            return [Recipient(ref=to, name=contact.display_name)] if contact else []
        people = await self._memory.find_people(user_id=user, name=to, run_id=identity.run_id)
        if people:
            found = [
                Recipient(
                    ref=person.model_ref,
                    name=person.display_name,
                    username=_username(person.identifiers),
                    relation=person.relation,
                )
                for person in people
            ]
            exact = [r for r in found if r.name.casefold() == to.casefold()]
            return exact if len(exact) == 1 else found
        return await self._phone_contacts(identity, to)

    async def _person_by_id(self, user_id: str, person_id: str) -> list[Recipient]:
        for person in await self._memory.list_people(user_id=user_id, status="active"):
            if str(person.id) == person_id.lower():
                return [
                    Recipient(
                        ref=person.model_ref,
                        name=person.display_name,
                        username=_username(person.identifiers),
                        relation=person.relation,
                    )
                ]
        return []

    async def _phone_contacts(self, identity: CallIdentity, name: str) -> list[Recipient]:
        run_id = identity.run_id
        device_id = await self._device_of(run_id)
        if run_id is None or device_id is None:
            return []
        declared = {
            tool.name: tool
            for tool in await self._channel.available_tools(
                user_id=identity.user_id, device_id=device_id
            )
        }
        declaration = declared.get(CONTACTS_TOOL)
        if declaration is None:
            return []
        result = await self._channel.invoke(
            user_id=identity.user_id,
            call=DeviceToolCall(
                call_id=f"tgc_{uuid.uuid4().hex}",
                run_id=run_id,
                device_id=device_id,
                tool=CONTACTS_TOOL,
                arguments={"query": name[:100]},
                deadline_at=self._clock() + timedelta(milliseconds=declaration.timeout_ms),
            ),
        )
        if result.status != "succeeded":
            return []
        contacts = parse_contacts(result.structured_content)
        refs = self._contacts.put(user_id=identity.user_id, run_id=run_id, contacts=contacts)
        found = [
            Recipient(ref=ref, name=contact.display_name)
            for ref, contact in zip(refs, contacts, strict=True)
        ]
        exact = [r for r in found if r.name.casefold() == name.casefold()]
        return exact if len(exact) == 1 else found

    # -- hand-off -------------------------------------------------------------------------

    async def _device_of(self, run_id: str | None) -> str | None:
        if run_id is None:
            return None
        try:
            ident = uuid.UUID(run_id)
        except ValueError:
            return None
        async with self._sessions() as session:
            device = await session.scalar(select(RunRow.device_id).where(RunRow.id == ident))
        return None if device is None else str(device)

    async def hand_off(
        self, identity: CallIdentity, recipient: Recipient, text: str
    ) -> HandoffOutcome:
        device_id = await self._device_of(identity.run_id)
        if device_id is None or identity.run_id is None:
            raise HostUnavailableError(NO_PHONE)
        declared = {
            tool.name: tool
            for tool in await self._channel.available_tools(
                user_id=identity.user_id, device_id=device_id
            )
        }
        declaration = declared.get(HANDOFF_TOOL)
        if declaration is None:
            return HandoffOutcome("failed", DEVICE_ERRORS["unsupported_tool"])
        arguments: dict[str, object] = {"text": text}
        if recipient.username:
            arguments["username"] = recipient.username
        else:
            arguments["contact_name"] = recipient.name[:100]
        result = await self._channel.invoke(
            user_id=identity.user_id,
            call=DeviceToolCall(
                call_id=f"tgh_{uuid.uuid4().hex}",
                run_id=identity.run_id,
                device_id=device_id,
                tool=HANDOFF_TOOL,
                arguments=arguments,
                deadline_at=self._clock() + timedelta(milliseconds=declaration.timeout_ms),
            ),
        )
        return _outcome(recipient, result)

    # -- bot ------------------------------------------------------------------------------

    async def bot_link(self, identity: CallIdentity) -> BotLinkOffer:
        stack = self._telegram
        if not stack.configured or stack.webhook_secret is None:
            raise HostUnavailableError("Telegram-бот Mia не настроен на этом сервере.")
        user = uuid.UUID(identity.user_id)
        if await stack.links.chat_for(user) is not None:
            return BotLinkOffer(already_linked=True)
        offer = await stack.links.create_offer(user)
        return BotLinkOffer(
            already_linked=False,
            deep_link=stack.deep_link(offer.token),
            expires_at=offer.expires_at.isoformat().replace("+00:00", "Z"),
        )


def _username(identifiers: object) -> str | None:
    value = identifiers.get("telegram_username") if hasattr(identifiers, "get") else None
    return value.lstrip("@") if isinstance(value, str) and value.lstrip("@") else None


def _outcome(recipient: Recipient, result: DeviceToolResult) -> HandoffOutcome:
    if result.status == "succeeded":
        direct = bool(result.structured_content and result.structured_content.get("direct_chat"))
        where = (
            f"Telegram открыт на чате с {recipient.name}, текст уже вписан."
            if direct
            else f"Telegram открыт с готовым текстом: пользователь выбирает чат {recipient.name}."
        )
        return HandoffOutcome(
            "opened",
            where + " Сообщение НЕ отправлено: пользователь нажимает «Отправить» сам.",
            direct_chat=direct,
        )
    if result.status == "uncertain":
        return HandoffOutcome(
            "uncertain",
            "Телефон не ответил вовремя: неизвестно, открылся ли Telegram. "
            "Ничего не отправлено; не повторяй сам, спроси пользователя.",
        )
    code = result.error_code or "execution_failed"
    if code == "invalid_arguments" and result.error_message:
        return HandoffOutcome(
            "failed",
            f"Телефон не смог выбрать контакт: {result.error_message} Ничего не отправлено.",
        )
    return HandoffOutcome(
        "failed",
        DEVICE_ERRORS.get(code, "Не удалось открыть Telegram на телефоне.")
        + " Ничего не отправлено.",
    )
