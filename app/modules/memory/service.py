"""Memory Ledger service: the ``MemoryProvider`` port plus the user-facing operations.

Rules (ADR-0012, Review §7.3/§7.5):

- every write from a model, app or import lands as ``suggested`` unless the user
  enabled auto-save for that (non-sensitive) category; confirming a fact that
  names a person creates or updates that ``Person``;
- the model context is the pinned profile plus the top FTS matches (blind
  index) and the people named in the query, all passed through ``minimize``:
  no phones, e-mails, handles or ids reach a provider; people appear only as
  ``person:<id>`` with name and relation;
- content is encrypted per user (``cipher.UserKeys``); logs never carry it;
- every read for a run, export, import or erase is written to the access log.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import Select, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY, TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.expression import cast as sql_cast
from sqlalchemy.types import Text

from app.core.ports.memory_provider import (
    FactRecord,
    MemoryCategory,
    MemorySource,
    PersonRef,
    RecordStatus,
    Sensitivity,
    SourceType,
)
from app.modules.conversations.models import IdempotencyRecord
from app.modules.memory.cipher import MemoryCipher, UserKeys
from app.modules.memory.domain import (
    CATEGORIES,
    AccessEntry,
    Fact,
    FactScope,
    HistoryRetention,
    Person,
    PersonInput,
    Preference,
    PrivacySettings,
    as_category,
    clean_identifiers,
    sensitivity_for,
)
from app.modules.memory.errors import (
    IdempotencyConflictError,
    InvalidMemoryRequestError,
    RecordNotFoundError,
)
from app.modules.memory.models import (
    MemoryAccessLog,
    MemoryFact,
    MemoryPerson,
    MemoryPreference,
    MemorySettings,
)
from app.modules.memory.text import PersonDraft, extract_person, minimize, name_score, search_terms

logger = logging.getLogger(__name__)

PINNED_PROFILE_MAX: Final = 5
PEOPLE_SCAN_MAX: Final = 1000
RESOLVE_MAX: Final = 5
CONTEXT_PEOPLE_MAX: Final = 3
Actor = Literal["run", "user", "app", "system"]


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _request_hash(body: Any) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _tsvector(tokens: list[str]) -> Any:
    return func.array_to_tsvector(literal(tokens, type_=ARRAY(Text)))


def _tsquery(tokens: list[str]) -> Any:
    # Tokens are ``t`` + hex (``UserKeys.tokens``): safe to quote, no normalization on cast.
    return sql_cast(" | ".join(f"'{token}'" for token in tokens), TSQUERY)


def encode_cursor(created_at: datetime, record_id: uuid.UUID) -> str:
    raw = f"{created_at.astimezone(UTC).isoformat()}|{record_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        stamp, _, ident = raw.partition("|")
        return datetime.fromisoformat(stamp), uuid.UUID(ident)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise InvalidMemoryRequestError("The cursor is invalid.") from None


@dataclass(frozen=True, slots=True)
class FactPage:
    items: list[Fact]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ImportResult:
    batch_id: uuid.UUID
    facts: int
    people: int

    def contract(self) -> dict[str, Any]:
        return {"batch_id": str(self.batch_id), "facts": self.facts, "people": self.people}


@dataclass(frozen=True, slots=True)
class ImportItems:
    facts: list[tuple[str, MemoryCategory]]
    people: list[PersonInput]


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    facts: list[Fact]
    people: list[Person]
    preferences: list[Preference]


class MemoryService:
    """Implements ``app.core.ports.memory_provider.MemoryProvider``."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: MemoryCipher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = session_factory
        self._cipher = cipher
        self._clock = clock

    # -- decoding ------------------------------------------------------------------

    def _fact(self, keys: UserKeys, row: MemoryFact) -> Fact:
        return Fact(
            id=row.id,
            text=keys.open_text(row.text_ciphertext, purpose="fact.text", record_id=row.id),
            category=as_category(row.category),
            status=cast(RecordStatus, row.status),
            sensitivity=cast(Sensitivity, row.sensitivity),
            scope=cast(FactScope, row.scope),
            source=MemorySource(type=cast(SourceType, row.source_type), ref=row.source_ref),
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_used_at=row.last_used_at,
            person_id=row.person_id,
        )

    def _person(self, keys: UserKeys, row: MemoryPerson) -> Person:
        raw = keys.open_json(row.payload_ciphertext, purpose="person", record_id=row.id)
        data = PersonInput.from_payload(raw)
        return Person(
            id=row.id,
            display_name=data.display_name,
            aliases=data.aliases,
            relation=data.relation,
            identifiers=data.identifiers,
            trusted=row.trusted,
            status=cast(RecordStatus, row.status),
            sensitivity=cast(Sensitivity, row.sensitivity),
            source=MemorySource(type=cast(SourceType, row.source_type), ref=row.source_ref),
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_used_at=row.last_used_at,
        )

    def _preference(self, keys: UserKeys, row: MemoryPreference) -> Preference:
        return Preference(
            id=row.id,
            key=row.key,
            value=keys.open_json(row.value_ciphertext, purpose="preference", record_id=row.id),
            source=MemorySource(type=cast(SourceType, row.source_type), ref=row.source_ref),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _fact_index(self, keys: UserKeys, text: str, person: PersonInput | None) -> Any:
        terms = search_terms(text)
        if person is not None:
            terms += search_terms(" ".join([person.display_name, *person.aliases]))
            if person.relation:
                terms += search_terms(person.relation)
        return _tsvector(keys.tokens(terms))

    # -- access log ----------------------------------------------------------------

    async def _log(
        self,
        session: AsyncSession,
        user: uuid.UUID,
        *,
        actor: Actor,
        action: str,
        count: int = 0,
        run_id: str | None = None,
        app_client_id: str | None = None,
    ) -> None:
        session.add(
            MemoryAccessLog(
                id=uuid.uuid4(),
                user_id=user,
                at=self._clock(),
                actor=actor,
                action=action,
                record_count=count,
                run_id=_uuid(run_id) if run_id else None,
                app_client_id=app_client_id,
            )
        )

    async def access_log(self, *, user_id: str, limit: int = 100) -> list[AccessEntry]:
        user = _required_uuid(user_id)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryAccessLog)
                    .where(MemoryAccessLog.user_id == user)
                    .order_by(MemoryAccessLog.at.desc(), MemoryAccessLog.id.desc())
                    .limit(limit)
                )
            ).all()
        return [
            AccessEntry(r.at, r.actor, r.action, r.record_count, r.run_id, r.app_client_id)
            for r in rows
        ]

    # -- settings ------------------------------------------------------------------

    async def _settings(self, session: AsyncSession, user: uuid.UUID) -> PrivacySettings:
        row = await session.get(MemorySettings, user)
        if row is None:
            return PrivacySettings()
        return PrivacySettings(
            autosave_categories=frozenset(
                as_category(c) for c in row.autosave_categories if c in CATEGORIES
            ),
            history_retention=cast(HistoryRetention, row.history_retention),
        )

    async def settings(self, *, user_id: str) -> PrivacySettings:
        async with self._sessions() as session:
            return await self._settings(session, _required_uuid(user_id))

    async def update_settings(
        self,
        *,
        user_id: str,
        autosave_categories: Collection[MemoryCategory] | None = None,
        history_retention: HistoryRetention | None = None,
    ) -> PrivacySettings:
        user = _required_uuid(user_id)
        if autosave_categories is not None:
            blocked = sorted(c for c in autosave_categories if sensitivity_for(c) == "sensitive")
            if blocked:
                raise InvalidMemoryRequestError(
                    "Sensitive categories cannot be saved automatically.",
                    details={"categories": blocked},
                )
        async with self._sessions() as session, session.begin():
            row = await session.get(MemorySettings, user, with_for_update=True)
            if row is None:
                row = MemorySettings(
                    user_id=user, autosave_categories=[], history_retention="forever"
                )
                session.add(row)
            if autosave_categories is not None:
                row.autosave_categories = sorted(set(autosave_categories))
            if history_retention is not None:
                row.history_retention = history_retention
            row.updated_at = self._clock()
            await session.flush()
            return await self._settings(session, user)

    # -- MemoryProvider: context ---------------------------------------------------

    async def context(
        self,
        *,
        user_id: str,
        query: str,
        categories: Collection[MemoryCategory] | None = None,
        limit: int = 8,
    ) -> Sequence[FactRecord]:
        """Pinned profile + top FTS facts + people named in the query, minimized."""
        user = _uuid(user_id)
        if user is None or limit <= 0:
            return ()
        async with self._sessions() as session, session.begin():
            maybe_keys = await self._cipher.existing_keys(session, user)
            if maybe_keys is None:
                return ()
            keys = maybe_keys
            chosen: list[FactRecord] = []
            seen: set[uuid.UUID] = set()
            allowed = set(categories) if categories is not None else None

            def add(row: MemoryFact) -> None:
                if row.id in seen or len(chosen) >= limit:
                    return
                fact = self._fact(keys, row)
                seen.add(row.id)
                chosen.append(
                    FactRecord(
                        id=str(fact.id),
                        text=minimize(fact.text),
                        category=fact.category,
                        status=fact.status,
                        sensitivity=fact.sensitivity,
                        source=fact.source,
                    )
                )

            people = await self._people_for_query(session, keys, user, query)
            people_slots = min(len(people), CONTEXT_PEOPLE_MAX, max(0, limit - 1))
            fact_limit = limit - people_slots
            if allowed is None or "profile" in allowed:
                pinned = (
                    await session.scalars(
                        self._active_facts(user)
                        .where(MemoryFact.category == "profile")
                        .where(MemoryFact.sensitivity == "normal")
                        .order_by(MemoryFact.updated_at.desc())
                        .limit(min(PINNED_PROFILE_MAX, fact_limit))
                    )
                ).all()
                for row in pinned:
                    add(row)
            tokens = keys.tokens(search_terms(query))
            if tokens and len(chosen) < fact_limit:
                query_ts = _tsquery(tokens)
                statement = (
                    self._active_facts(user)
                    .where(MemoryFact.search_index.op("@@")(query_ts))
                    .order_by(
                        func.ts_rank(MemoryFact.search_index, query_ts).desc(),
                        MemoryFact.updated_at.desc(),
                    )
                    .limit(fact_limit + len(seen))
                )
                if allowed is not None:
                    statement = statement.where(MemoryFact.category.in_(sorted(allowed)))
                for row in (await session.scalars(statement)).all():
                    if len(chosen) >= fact_limit:
                        break
                    add(row)
            for person in people[:people_slots]:
                # Only the name and relation are minimized: the reference itself is an id.
                line = f"{person.model_ref}: {minimize(person.display_name)}"
                if person.relation:
                    line += f" ({minimize(person.relation)})"
                chosen.append(
                    FactRecord(
                        id=str(person.id),
                        text=line,
                        category="other",
                        status="active",
                        sensitivity=person.sensitivity,
                        source=person.source,
                    )
                )
            await self._log(session, user, actor="run", action="context", count=len(chosen))
        return chosen

    def _active_facts(self, user: uuid.UUID) -> Select[tuple[MemoryFact]]:
        return select(MemoryFact).where(MemoryFact.user_id == user, MemoryFact.status == "active")

    async def _people_rows(
        self, session: AsyncSession, user: uuid.UUID, *, statuses: Sequence[str] = ("active",)
    ) -> list[MemoryPerson]:
        return list(
            (
                await session.scalars(
                    select(MemoryPerson)
                    .where(MemoryPerson.user_id == user, MemoryPerson.status.in_(statuses))
                    .order_by(MemoryPerson.created_at, MemoryPerson.id)
                    .limit(PEOPLE_SCAN_MAX)
                )
            ).all()
        )

    async def _people_for_query(
        self, session: AsyncSession, keys: UserKeys, user: uuid.UUID, query: str
    ) -> list[Person]:
        terms = set(search_terms(query))
        if not terms:
            return []
        matched: list[Person] = []
        for row in await self._people_rows(session, user):
            person = self._person(keys, row)
            names = set(search_terms(" ".join(person.names)))
            if terms & names:
                matched.append(person)
        return matched

    # -- MemoryProvider: writes and people -----------------------------------------

    async def suggest(
        self, *, user_id: str, text: str, category: MemoryCategory, source: MemorySource
    ) -> FactRecord:
        fact = await self.remember(user_id=user_id, text=text, category=category, source=source)
        return FactRecord(
            id=str(fact.id),
            text=fact.text,
            category=fact.category,
            status=fact.status,
            sensitivity=fact.sensitivity,
            source=fact.source,
        )

    async def remember(
        self,
        *,
        user_id: str,
        text: str,
        category: MemoryCategory,
        source: MemorySource,
        person: PersonInput | None = None,
        actor: Actor = "run",
        run_id: str | None = None,
        app_client_id: str | None = None,
        force_suggested: bool = False,
    ) -> Fact:
        """Store a fact as ``suggested`` (or ``active`` if auto-save allows it)."""
        user = _required_uuid(user_id)
        text = text.strip()
        if not text or len(text) > 2000:
            raise InvalidMemoryRequestError("The fact text must be 1-2000 characters.")
        if person is None:
            draft = extract_person(text)
            person = _from_draft(draft) if draft else None
        sensitivity = sensitivity_for(category)
        async with self._sessions() as session, session.begin():
            settings = await self._settings(session, user)
            auto = (
                not force_suggested
                and sensitivity == "normal"
                and category in settings.autosave_categories
                and source.type not in ("import", "app")
            )
            fact = await self._insert_fact(
                session,
                user,
                text=text,
                category=category,
                status="active" if auto else "suggested",
                source=source,
                person=person,
            )
            await self._log(
                session,
                user,
                actor=actor,
                action="remember" if auto else "suggest",
                count=1,
                run_id=run_id,
                app_client_id=app_client_id,
            )
            return fact

    async def _insert_fact(
        self,
        session: AsyncSession,
        user: uuid.UUID,
        *,
        text: str,
        category: MemoryCategory,
        status: RecordStatus,
        source: MemorySource,
        person: PersonInput | None,
        scope: FactScope = "user",
    ) -> Fact:
        keys = await self._cipher.keys(session, user)
        now = self._clock()
        fact_id = uuid.uuid4()
        person_id: uuid.UUID | None = None
        draft_blob: bytes | None = None
        if person is not None:
            if status == "active":
                person_id = (await self._upsert_person(session, keys, user, person, source)).id
            else:
                draft_blob = keys.seal_json(
                    person.payload(), purpose="fact.person_draft", record_id=fact_id
                )
        row = MemoryFact(
            id=fact_id,
            user_id=user,
            category=category,
            status=status,
            sensitivity=sensitivity_for(category),
            scope=scope,
            source_type=source.type,
            source_ref=source.ref,
            text_ciphertext=keys.seal_text(text, purpose="fact.text", record_id=fact_id),
            person_draft_ciphertext=draft_blob,
            person_id=person_id,
            search_index=self._fact_index(keys, text, person),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return self._fact(keys, row)

    async def _upsert_person(
        self,
        session: AsyncSession,
        keys: UserKeys,
        user: uuid.UUID,
        data: PersonInput,
        source: MemorySource,
        *,
        status: RecordStatus = "active",
    ) -> Person:
        """Merge into an active person with the same name, else create one."""
        wanted = data.display_name.strip().lower()
        if status == "active":
            for row in await self._people_rows(session, user):
                existing = self._person(keys, row)
                if existing.display_name.strip().lower() != wanted:
                    continue
                aliases = list(existing.aliases)
                for alias in data.aliases:
                    if alias not in aliases and alias != existing.display_name:
                        aliases.append(alias)
                merged = PersonInput(
                    display_name=existing.display_name,
                    relation=existing.relation or data.relation,
                    aliases=tuple(aliases[:20]),
                    identifiers={**dict(data.identifiers), **dict(existing.identifiers)},
                )
                row.payload_ciphertext = keys.seal_json(
                    merged.payload(), purpose="person", record_id=row.id
                )
                row.updated_at = self._clock()
                await session.flush()
                return self._person(keys, row)
        now = self._clock()
        person_id = uuid.uuid4()
        clean = PersonInput(
            display_name=data.display_name.strip()[:120],
            relation=(data.relation or "").strip()[:60] or None,
            aliases=tuple(a.strip()[:60] for a in data.aliases if a.strip())[:20],
            identifiers=clean_identifiers(data.identifiers),
        )
        row = MemoryPerson(
            id=person_id,
            user_id=user,
            status=status,
            sensitivity="normal",
            trusted=False,
            source_type=source.type,
            source_ref=source.ref,
            payload_ciphertext=keys.seal_json(
                clean.payload(), purpose="person", record_id=person_id
            ),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return self._person(keys, row)

    async def resolve_person(self, *, user_id: str, name: str) -> Sequence[PersonRef]:
        return [
            PersonRef(id=str(p.id), display_name=p.display_name, relation=p.relation)
            for p in await self.find_people(user_id=user_id, name=name)
        ]

    async def find_people(
        self, *, user_id: str, name: str, run_id: str | None = None
    ) -> list[Person]:
        user = _uuid(user_id)
        if user is None:
            return []
        async with self._sessions() as session, session.begin():
            keys = await self._cipher.existing_keys(session, user)
            if keys is None:
                return []
            scored: list[tuple[int, Person]] = []
            for row in await self._people_rows(session, user):
                person = self._person(keys, row)
                score = name_score(name, person.names, person.relation)
                if score:
                    scored.append((score, person))
            scored.sort(key=lambda item: (-item[0], item[1].created_at))
            found = [person for _, person in scored[:RESOLVE_MAX]]
            await self._log(
                session, user, actor="run", action="resolve", count=len(found), run_id=run_id
            )
        return found

    async def person_identifiers(self, *, user_id: str, person_id: str) -> Mapping[str, str | int]:
        user, ident = _uuid(user_id), _uuid(person_id)
        if user is None or ident is None:
            return {}
        async with self._sessions() as session, session.begin():
            row = await session.get(MemoryPerson, ident)
            if row is None or row.user_id != user or row.status != "active":
                return {}
            keys = await self._cipher.keys(session, user)
            return dict(self._person(keys, row).identifiers)

    async def mark_used(self, *, user_id: str, record_ids: Sequence[str]) -> None:
        user = _uuid(user_id)
        ids = [i for i in (_uuid(r) for r in record_ids) if i is not None]
        if user is None or not ids:
            return
        now = self._clock()
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(MemoryFact)
                .where(MemoryFact.user_id == user, MemoryFact.id.in_(ids))
                .values(last_used_at=now)
            )
            await session.execute(
                update(MemoryPerson)
                .where(MemoryPerson.user_id == user, MemoryPerson.id.in_(ids))
                .values(last_used_at=now)
            )

    async def render_context(self, *, user_id: str, query: str, limit: int = 8) -> str:
        """``GET /v1/memory/context``: the same lines a run would get."""
        facts = await self.context(user_id=user_id, query=query, limit=limit)
        return "\n".join(f"- [{fact.category}] {fact.text}" for fact in facts)

    # -- facts (API) ---------------------------------------------------------------

    async def list_facts(
        self,
        *,
        user_id: str,
        status: RecordStatus | None = None,
        category: MemoryCategory | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> FactPage:
        user = _required_uuid(user_id)
        statement = select(MemoryFact).where(MemoryFact.user_id == user)
        if status is not None:
            statement = statement.where(MemoryFact.status == status)
        if category is not None:
            statement = statement.where(MemoryFact.category == category)
        if cursor:
            created, ident = decode_cursor(cursor)
            statement = statement.where(
                (MemoryFact.created_at < created)
                | ((MemoryFact.created_at == created) & (MemoryFact.id < ident))
            )
        statement = statement.order_by(MemoryFact.created_at.desc(), MemoryFact.id.desc()).limit(
            limit + 1
        )
        async with self._sessions() as session, session.begin():
            rows = list((await session.scalars(statement)).all())
            keys = await self._cipher.existing_keys(session, user) if rows else None
            items = [self._fact(keys, row) for row in rows[:limit]] if keys else []
        next_cursor = (
            encode_cursor(rows[limit - 1].created_at, rows[limit - 1].id)
            if len(rows) > limit
            else None
        )
        return FactPage(items=items, next_cursor=next_cursor)

    async def create_fact(
        self,
        *,
        user_id: str,
        text: str,
        category: MemoryCategory,
        idempotency_key: str | None = None,
    ) -> tuple[Fact, bool]:
        """User-authored fact: ``active`` at once. Returns ``(fact, created)``."""
        user = _required_uuid(user_id)
        text = text.strip()
        if not text:
            raise InvalidMemoryRequestError("The fact text must not be empty.")
        async with self._sessions() as session, session.begin():
            replay = await self._replay(
                session, user, "memory.facts.create", idempotency_key, [text, category]
            )
            if replay is not None:
                row = await session.get(MemoryFact, replay)
                if row is None or row.user_id != user:
                    raise RecordNotFoundError()
                return self._fact(await self._cipher.keys(session, user), row), False
            draft = extract_person(text)
            fact = await self._insert_fact(
                session,
                user,
                text=text,
                category=category,
                status="active",
                source=MemorySource("user_said"),
                person=_from_draft(draft) if draft else None,
            )
            await self._remember_key(
                session, user, "memory.facts.create", idempotency_key, [text, category], fact.id
            )
            await self._log(session, user, actor="user", action="create", count=1)
            return fact, True

    async def update_fact(
        self,
        *,
        user_id: str,
        fact_id: str,
        text: str | None = None,
        category: MemoryCategory | None = None,
        status: Literal["active", "rejected"] | None = None,
        scope: FactScope | None = None,
    ) -> Fact:
        """Edit, confirm (``status=active``) or reject a fact."""
        user, ident = _required_uuid(user_id), _uuid(fact_id)
        async with self._sessions() as session, session.begin():
            row = await session.get(MemoryFact, ident, with_for_update=True) if ident else None
            if row is None or row.user_id != user:
                raise RecordNotFoundError()
            keys = await self._cipher.keys(session, user)
            current = self._fact(keys, row)
            draft: PersonInput | None = None
            if row.person_draft_ciphertext is not None:
                draft = PersonInput.from_payload(
                    keys.open_json(
                        row.person_draft_ciphertext,
                        purpose="fact.person_draft",
                        record_id=row.id,
                    )
                )
            new_text = text.strip() if text is not None else current.text
            if not new_text:
                raise InvalidMemoryRequestError("The fact text must not be empty.")
            if text is not None:
                row.text_ciphertext = keys.seal_text(
                    new_text, purpose="fact.text", record_id=row.id
                )
            if category is not None:
                row.category = category
                row.sensitivity = sensitivity_for(category)
            if scope is not None:
                row.scope = scope
            if status is not None:
                row.status = status
            if status == "active" and draft is not None:
                origin = MemorySource(cast(SourceType, row.source_type), row.source_ref)
                person = await self._upsert_person(session, keys, user, draft, origin)
                row.person_id = person.id
                row.person_draft_ciphertext = None
            elif status == "rejected":
                row.person_draft_ciphertext = None
            row.search_index = self._fact_index(keys, new_text, draft)
            row.updated_at = self._clock()
            await session.flush()
            await session.refresh(row)
            action = {"active": "confirm", "rejected": "reject"}.get(status or "", "edit")
            await self._log(session, user, actor="user", action=action, count=1)
            return self._fact(keys, row)

    async def delete_fact(self, *, user_id: str, fact_id: str) -> None:
        user, ident = _required_uuid(user_id), _uuid(fact_id)
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                delete(MemoryFact).where(MemoryFact.user_id == user, MemoryFact.id == ident)
            )
            if not getattr(result, "rowcount", 0):
                raise RecordNotFoundError()
            await self._log(session, user, actor="user", action="delete", count=1)

    # -- people (API) --------------------------------------------------------------

    async def list_people(
        self, *, user_id: str, status: RecordStatus | None = None
    ) -> list[Person]:
        user = _required_uuid(user_id)
        statuses: Sequence[str] = (status,) if status else ("suggested", "active", "rejected")
        async with self._sessions() as session, session.begin():
            keys = await self._cipher.existing_keys(session, user)
            if keys is None:
                return []
            return [
                self._person(keys, row)
                for row in await self._people_rows(session, user, statuses=statuses)
            ]

    async def create_person(
        self,
        *,
        user_id: str,
        data: PersonInput,
        trusted: bool = False,
        idempotency_key: str | None = None,
        source: MemorySource | None = None,
        status: RecordStatus = "active",
    ) -> tuple[Person, bool]:
        user = _required_uuid(user_id)
        body = [data.payload(), trusted]
        async with self._sessions() as session, session.begin():
            keys = await self._cipher.keys(session, user)
            replay = await self._replay(
                session, user, "memory.people.create", idempotency_key, body
            )
            if replay is not None:
                row = await session.get(MemoryPerson, replay)
                if row is None or row.user_id != user:
                    raise RecordNotFoundError()
                return self._person(keys, row), False
            person = await self._create_person_row(
                session, keys, user, data, source or MemorySource("user_said"), status, trusted
            )
            await self._remember_key(
                session, user, "memory.people.create", idempotency_key, body, person.id
            )
            await self._log(session, user, actor="user", action="create", count=1)
            return person, True

    async def _create_person_row(
        self,
        session: AsyncSession,
        keys: UserKeys,
        user: uuid.UUID,
        data: PersonInput,
        source: MemorySource,
        status: RecordStatus,
        trusted: bool,
    ) -> Person:
        if status == "active":
            person = await self._upsert_person(session, keys, user, data, source)
        else:
            person = await self._upsert_person(session, keys, user, data, source, status=status)
        if trusted and not person.trusted:
            await session.execute(
                update(MemoryPerson).where(MemoryPerson.id == person.id).values(trusted=True)
            )
            person = replace(person, trusted=True)
        return person

    async def update_person(
        self, *, user_id: str, person_id: str, changes: Mapping[str, Any]
    ) -> Person:
        user, ident = _required_uuid(user_id), _uuid(person_id)
        async with self._sessions() as session, session.begin():
            row = await session.get(MemoryPerson, ident, with_for_update=True) if ident else None
            if row is None or row.user_id != user:
                raise RecordNotFoundError()
            keys = await self._cipher.keys(session, user)
            current = self._person(keys, row)
            payload = PersonInput(
                display_name=current.display_name,
                relation=current.relation,
                aliases=current.aliases,
                identifiers=current.identifiers,
            ).payload()
            for key in ("display_name", "relation", "aliases"):
                if key in changes:
                    payload[key] = changes[key]
            if "identifiers" in changes:
                payload["identifiers"] = clean_identifiers(changes["identifiers"])
            row.payload_ciphertext = keys.seal_json(
                PersonInput.from_payload(payload).payload(), purpose="person", record_id=row.id
            )
            if "trusted" in changes:
                row.trusted = bool(changes["trusted"])
            if "status" in changes:
                row.status = changes["status"]
            row.updated_at = self._clock()
            await session.flush()
            await session.refresh(row)
            await self._log(session, user, actor="user", action="edit", count=1)
            return self._person(keys, row)

    async def delete_person(self, *, user_id: str, person_id: str) -> None:
        user, ident = _required_uuid(user_id), _uuid(person_id)
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                delete(MemoryPerson).where(MemoryPerson.user_id == user, MemoryPerson.id == ident)
            )
            if not getattr(result, "rowcount", 0):
                raise RecordNotFoundError()
            await self._log(session, user, actor="user", action="delete", count=1)

    # -- preferences ---------------------------------------------------------------

    async def list_preferences(self, *, user_id: str) -> list[Preference]:
        user = _required_uuid(user_id)
        async with self._sessions() as session, session.begin():
            keys = await self._cipher.existing_keys(session, user)
            if keys is None:
                return []
            rows = (
                await session.scalars(
                    select(MemoryPreference)
                    .where(MemoryPreference.user_id == user)
                    .order_by(MemoryPreference.key)
                )
            ).all()
            return [self._preference(keys, row) for row in rows]

    async def put_preference(
        self,
        *,
        user_id: str,
        key: str,
        value: str | int | float | bool,
        source: MemorySource | None = None,
    ) -> Preference:
        user = _required_uuid(user_id)
        source = source or MemorySource("user_said")
        async with self._sessions() as session, session.begin():
            keys = await self._cipher.keys(session, user)
            row = await session.scalar(
                select(MemoryPreference)
                .where(MemoryPreference.user_id == user, MemoryPreference.key == key)
                .with_for_update()
            )
            now = self._clock()
            if row is None:
                row = MemoryPreference(
                    id=uuid.uuid4(),
                    user_id=user,
                    key=key,
                    created_at=now,
                    source_type=source.type,
                    source_ref=source.ref,
                    value_ciphertext=b"",
                )
                session.add(row)
            row.value_ciphertext = keys.seal_json(value, purpose="preference", record_id=row.id)
            row.updated_at = now
            await session.flush()
            await session.refresh(row)
            await self._log(session, user, actor="user", action="edit", count=1)
            return self._preference(keys, row)

    # -- export / import / erase ---------------------------------------------------

    async def snapshot(self, *, user_id: str, actor: Actor = "user") -> LedgerSnapshot:
        user = _required_uuid(user_id)
        async with self._sessions() as session, session.begin():
            keys = await self._cipher.existing_keys(session, user)
            if keys is None:
                return LedgerSnapshot(facts=[], people=[], preferences=[])
            facts = (
                await session.scalars(
                    select(MemoryFact)
                    .where(MemoryFact.user_id == user)
                    .order_by(MemoryFact.created_at, MemoryFact.id)
                )
            ).all()
            people = await self._people_rows(
                session, user, statuses=("suggested", "active", "rejected")
            )
            preferences = (
                await session.scalars(
                    select(MemoryPreference)
                    .where(MemoryPreference.user_id == user)
                    .order_by(MemoryPreference.key)
                )
            ).all()
            snapshot = LedgerSnapshot(
                facts=[self._fact(keys, row) for row in facts],
                people=[self._person(keys, row) for row in people],
                preferences=[self._preference(keys, row) for row in preferences],
            )
            total = len(snapshot.facts) + len(snapshot.people) + len(snapshot.preferences)
            await self._log(session, user, actor=actor, action="export", count=total)
            return snapshot

    async def import_items(
        self,
        *,
        user_id: str,
        items: ImportItems,
        idempotency_key: str | None = None,
        request_body: Any = None,
    ) -> ImportResult:
        """Everything imported lands in the review queue (``suggested``)."""
        user = _required_uuid(user_id)
        async with self._sessions() as session, session.begin():
            replay = await self._replay(
                session, user, "memory.import", idempotency_key, request_body
            )
            if replay is not None:
                return await self._import_counts(session, user, replay)
            batch = uuid.uuid4()
            source = MemorySource("import", ref=str(batch))
            keys = await self._cipher.keys(session, user)
            for text, category in items.facts:
                await self._insert_fact(
                    session,
                    user,
                    text=text,
                    category=category,
                    status="suggested",
                    source=source,
                    person=None,
                )
            for person in items.people:
                await self._upsert_person(session, keys, user, person, source, status="suggested")
            await self._remember_key(
                session, user, "memory.import", idempotency_key, request_body, batch
            )
            count = len(items.facts) + len(items.people)
            await self._log(session, user, actor="user", action="import", count=count)
            return ImportResult(batch, len(items.facts), len(items.people))

    async def _import_counts(
        self, session: AsyncSession, user: uuid.UUID, batch: uuid.UUID
    ) -> ImportResult:
        facts = await session.scalar(
            select(func.count())
            .select_from(MemoryFact)
            .where(MemoryFact.user_id == user, MemoryFact.source_ref == str(batch))
        )
        people = await session.scalar(
            select(func.count())
            .select_from(MemoryPerson)
            .where(MemoryPerson.user_id == user, MemoryPerson.source_ref == str(batch))
        )
        return ImportResult(batch, int(facts or 0), int(people or 0))

    async def forget_all(self, *, user_id: str) -> int:
        """«Удалить всю память»: hard-delete facts, people and preferences."""
        user = _required_uuid(user_id)
        async with self._sessions() as session, session.begin():
            total = 0
            for model in (MemoryFact, MemoryPerson, MemoryPreference):
                result = await session.execute(delete(model).where(model.user_id == user))
                total += int(getattr(result, "rowcount", 0) or 0)
            await self._log(session, user, actor="user", action="erase", count=total)
            return total

    async def shred_account(self, *, user_id: str) -> bool:
        """Account deletion: crypto-shred the user's DEK and delete every memory row.

        Ciphertext that survives elsewhere (backups, replicas) becomes unreadable.
        The ``users`` row itself is deleted by the account owner flow (cascade).
        """
        user = _required_uuid(user_id)
        async with self._sessions() as session, session.begin():
            for model in (MemoryFact, MemoryPerson, MemoryPreference, MemoryAccessLog):
                await session.execute(delete(model).where(model.user_id == user))
            await session.execute(delete(MemorySettings).where(MemorySettings.user_id == user))
            shredded = await self._cipher.shred(session, user)
        logger.info("memory_crypto_shred", extra={"shredded": shredded})
        return shredded

    # -- idempotency ---------------------------------------------------------------

    async def _replay(
        self,
        session: AsyncSession,
        user: uuid.UUID,
        endpoint: str,
        key: str | None,
        body: Any,
    ) -> uuid.UUID | None:
        if not key:
            return None
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.user_id == user,
                IdempotencyRecord.endpoint == endpoint,
                IdempotencyRecord.idempotency_key == key,
            )
        )
        if record is None:
            return None
        if record.request_hash != _request_hash(body):
            raise IdempotencyConflictError()
        return record.resource_id

    async def _remember_key(
        self,
        session: AsyncSession,
        user: uuid.UUID,
        endpoint: str,
        key: str | None,
        body: Any,
        resource_id: uuid.UUID,
    ) -> None:
        if not key:
            return
        session.add(
            IdempotencyRecord(
                id=uuid.uuid4(),
                user_id=user,
                endpoint=endpoint,
                idempotency_key=key,
                request_hash=_request_hash(body),
                resource_kind=endpoint.split(".")[1],
                resource_id=resource_id,
            )
        )
        await session.flush()


def _required_uuid(value: str) -> uuid.UUID:
    parsed = _uuid(value)
    if parsed is None:
        raise RecordNotFoundError()
    return parsed


def _from_draft(draft: PersonDraft) -> PersonInput:
    return PersonInput(display_name=draft.display_name, relation=draft.relation)


def parse_category(value: str | None, default: MemoryCategory = "other") -> MemoryCategory:
    return as_category(value) if value in CATEGORIES else default
