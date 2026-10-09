"""Core tools ``memory.remember`` and ``people.resolve`` (M13 core-tool extension point).

- ``memory.remember`` stores a fact as *suggested* (one-tap confirmation) or
  *active* when auto-save is on for that non-sensitive category, and publishes
  ``memory.suggested`` into the run stream. Incognito runs never write.
- ``people.resolve`` looks a spoken name up in memory first, then (only if
  memory has nobody) in the phone's contacts via the device tool
  ``device.contacts.search`` (``DeviceToolChannel``). The model gets references
  only — ``person:<id>`` for memory, ``contact:<n>`` for a contact of this run —
  with display name and relation; phone numbers stay in the core. Contact
  details live in process memory for the run (``ContactsCache``) so «запомни
  её» can store them; they are never written unless the user confirms.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.device_tool_channel import DeviceToolCall, DeviceToolChannel
from app.core.ports.memory_provider import MemoryCategory, MemorySource
from app.core.ports.tool_router import ToolContext
from app.core.ports.types import JsonObject
from app.modules.memory.domain import CATEGORIES, PersonInput, clean_identifiers
from app.modules.memory.service import MemoryService
from app.modules.memory.text import minimize
from app.modules.runs.models import RunRow
from app.modules.tools.core_tools import CoreTool, CoreToolResult

logger = logging.getLogger(__name__)

REMEMBER_TOOL = "memory.remember"
RESOLVE_TOOL = "people.resolve"
CONTACTS_TOOL = "device.contacts.search"
CONTACTS_MAX = 5


class RunPrivacy(Protocol):
    async def is_incognito(self, run_id: str) -> bool | None:
        """True/False for a known run, None when the run is unknown."""
        ...


class SqlRunPrivacy:
    """Reads ``runs.incognito`` (M12) for the run that called a tool."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def is_incognito(self, run_id: str) -> bool | None:
        try:
            ident = uuid.UUID(run_id)
        except ValueError:
            return None
        async with self._sessions() as session:
            value = await session.scalar(select(RunRow.incognito).where(RunRow.id == ident))
        return None if value is None else bool(value)


class RunEventPublisher(Protocol):
    """``RunService.publish`` (M12)."""

    async def __call__(
        self, *, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any]
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class Contact:
    display_name: str
    identifiers: Mapping[str, str | int]


@dataclass(slots=True)
class _RunContacts:
    expires: float
    contacts: dict[str, Contact] = field(default_factory=dict)


class ContactsCache:
    """Contacts seen by one run, kept in process memory only, bounded and expiring."""

    def __init__(
        self,
        *,
        ttl_s: float = 900.0,
        max_runs: int = 1000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_s
        self._max = max_runs
        self._now = monotonic
        self._runs: OrderedDict[tuple[str, str], _RunContacts] = OrderedDict()

    def put(self, *, user_id: str, run_id: str, contacts: Sequence[Contact]) -> list[str]:
        self._evict()
        key = (user_id, run_id)
        entry = self._runs.get(key) or _RunContacts(expires=self._now() + self._ttl)
        refs = []
        for contact in contacts:
            ref = f"contact:{len(entry.contacts) + 1}"
            entry.contacts[ref] = contact
            refs.append(ref)
        self._runs[key] = entry
        self._runs.move_to_end(key)
        while len(self._runs) > self._max:
            self._runs.popitem(last=False)
        return refs

    def get(self, *, user_id: str, run_id: str, ref: str) -> Contact | None:
        self._evict()
        entry = self._runs.get((user_id, run_id))
        return entry.contacts.get(ref) if entry else None

    def _evict(self) -> None:
        now = self._now()
        for key in [k for k, v in self._runs.items() if v.expires <= now]:
            del self._runs[key]


def parse_contacts(structured: JsonObject | None) -> list[Contact]:
    """Tolerant reader of the ``device.contacts.search`` result (M23).

    Expected ``structured_content``: ``{"contacts": [{"display_name"|"name": str,
    "phones"|"phone": [...], "emails"|"email": [...]}]}``.
    """
    if not structured:
        return []
    raw = structured.get("contacts", structured.get("results"))
    if not isinstance(raw, list):
        return []
    contacts: list[Contact] = []
    for item in raw:
        if len(contacts) >= CONTACTS_MAX:
            break
        if not isinstance(item, Mapping):
            continue
        name = item.get("display_name") or item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        phones = item.get("phones", item.get("phone"))
        phone = phones[0] if isinstance(phones, list) and phones else phones
        if isinstance(phone, Mapping):
            phone = phone.get("number")
        emails = item.get("emails", item.get("email"))
        email = emails[0] if isinstance(emails, list) and emails else emails
        identifiers = clean_identifiers(
            {
                "phone": phone if isinstance(phone, str) else None,
                "email": email if isinstance(email, str) else None,
            }
        )
        contacts.append(Contact(display_name=name.strip()[:120], identifiers=identifiers))
    return contacts


def _result(
    body: Mapping[str, Any], *, status: str = "succeeded", code: str | None = None
) -> CoreToolResult:
    content = json.dumps(body, ensure_ascii=False)
    if status == "failed":
        return CoreToolResult(content=content, status="failed", error_code=code)
    return CoreToolResult(content=content, structured_content=dict(body))


REMEMBER_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "category"],
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "person": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "relation": {"type": "string", "minLength": 1, "maxLength": 60},
                "aliases": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "minLength": 1, "maxLength": 60},
                },
                "contact_ref": {"type": "string", "pattern": "^contact:[0-9]{1,3}$"},
            },
        },
    },
}

RESOLVE_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 120}},
}


class MemoryTools:
    def __init__(
        self,
        service: MemoryService,
        privacy: RunPrivacy,
        *,
        channel: DeviceToolChannel | None = None,
        contacts: ContactsCache | None = None,
        publisher: RunEventPublisher | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.service = service
        self.privacy = privacy
        self.channel = channel
        self.contacts = contacts or ContactsCache()
        self.publisher = publisher
        self.clock = clock

    def bind_publisher(self, publisher: RunEventPublisher) -> None:
        self.publisher = publisher

    def core_tools(self) -> list[CoreTool]:
        return [
            CoreTool(
                name=REMEMBER_TOOL,
                description=(
                    "Предложить запомнить факт о пользователе или человеке из его окружения. "
                    "Факт сохраняется только после подтверждения пользователем. Для человека "
                    "передай person.name и relation; contact_ref — ссылка из people.resolve."
                ),
                input_schema=REMEMBER_SCHEMA,
                risk="write_reversible",
                handler=self.remember,
                voice=True,
            ),
            CoreTool(
                name=RESOLVE_TOOL,
                description=(
                    "Найти человека по имени или отношению («Яна», «жена»): сначала в памяти Mia, "
                    "затем в контактах телефона. Возвращает ссылки person:<id> или contact:<n> "
                    "для других инструментов; телефоны и идентификаторы не возвращаются."
                ),
                input_schema=RESOLVE_SCHEMA,
                risk="read",
                handler=self.resolve,
                voice=True,
                timeout_s=15.0,
            ),
        ]

    async def remember(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        incognito = await self.privacy.is_incognito(context.run_id)
        if incognito is None:
            return _result({"saved": False}, status="failed", code="memory_unavailable")
        if incognito:
            return _result(
                {"saved": False, "reason": "incognito"}, status="failed", code="memory_incognito"
            )
        category: MemoryCategory = args["category"]  # type: ignore[assignment]
        person = self._person_input(context, args.get("person"))
        fact = await self.service.remember(
            user_id=context.user_id,
            text=str(args["text"]),
            category=category,
            source=MemorySource("turn", ref=context.run_id),
            person=person,
            actor="app" if context.profile == "app" else "run",
            run_id=context.run_id,
            app_client_id=context.app_client_id,
        )
        if fact.status == "suggested" and self.publisher is not None:
            try:
                await self.publisher(
                    run_id=uuid.UUID(context.run_id),
                    event_type="memory.suggested",
                    payload={"fact_id": str(fact.id), "text": fact.text, "category": category},
                )
            except Exception:  # the fact is stored; the client also lists suggestions
                logger.warning("memory_suggested_publish_failed")
        body: dict[str, Any] = {"fact_id": str(fact.id), "status": fact.status}
        body["note"] = (
            "Сохранено в память."
            if fact.status == "active"
            else "Предложено запомнить; пользователь подтвердит в приложении."
        )
        return _result(body)

    def _person_input(self, context: ToolContext, raw: object) -> PersonInput | None:
        if not isinstance(raw, Mapping):
            return None
        identifiers: Mapping[str, str | int] = {}
        name = str(raw["name"])
        ref = raw.get("contact_ref")
        if isinstance(ref, str):
            contact = self.contacts.get(user_id=context.user_id, run_id=context.run_id, ref=ref)
            if contact is not None:
                identifiers = contact.identifiers
        aliases = tuple(str(a) for a in raw.get("aliases") or ())
        relation = raw.get("relation")
        return PersonInput(
            display_name=name,
            relation=str(relation) if relation else None,
            aliases=aliases,
            identifiers=identifiers,
        )

    async def resolve(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        name = str(args["name"])
        incognito = await self.privacy.is_incognito(context.run_id)
        people = await self.service.find_people(
            user_id=context.user_id, name=name, run_id=context.run_id
        )
        candidates: list[dict[str, Any]] = []
        for person in people:
            candidate: dict[str, Any] = {
                "ref": person.model_ref,
                "name": minimize(person.display_name),
                "source": "memory",
            }
            if person.relation:
                candidate["relation"] = minimize(person.relation)
            candidates.append(candidate)
        if people and incognito is False:
            await self.service.mark_used(
                user_id=context.user_id, record_ids=[str(p.id) for p in people]
            )
        if not candidates:
            candidates = await self._search_contacts(context, name)
        body: dict[str, Any] = {"query": name, "candidates": candidates}
        if not candidates:
            body["note"] = "Никого не найдено."
        elif len(candidates) > 1:
            body["note"] = "Несколько вариантов: уточни у пользователя."
        return _result(body)

    async def _search_contacts(self, context: ToolContext, name: str) -> list[dict[str, Any]]:
        channel = self.channel
        if channel is None or context.device_id is None:
            return []
        declared = {
            tool.name: tool
            for tool in await channel.available_tools(
                user_id=context.user_id, device_id=context.device_id
            )
        }
        declaration = declared.get(CONTACTS_TOOL)
        if declaration is None:
            return []
        result = await channel.invoke(
            user_id=context.user_id,
            call=DeviceToolCall(
                call_id=f"mem_{uuid.uuid4().hex}",
                run_id=context.run_id,
                device_id=context.device_id,
                tool=CONTACTS_TOOL,
                arguments={"query": name[:100]},
                deadline_at=self.clock() + timedelta(milliseconds=declaration.timeout_ms),
            ),
        )
        if result.status != "succeeded":
            return []
        contacts = parse_contacts(result.structured_content)
        refs = self.contacts.put(user_id=context.user_id, run_id=context.run_id, contacts=contacts)
        return [
            {"ref": ref, "name": minimize(contact.display_name), "source": "device_contacts"}
            for ref, contact in zip(refs, contacts, strict=True)
        ]
