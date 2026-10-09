"""In-memory MemoryProvider: word-overlap search instead of Postgres FTS."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from app.core.ports.memory_provider import (
    FactRecord,
    MemoryCategory,
    MemorySource,
    PersonRef,
)


@dataclass
class FakePerson:
    ref: PersonRef
    aliases: tuple[str, ...] = ()
    identifiers: dict[str, str | int] = field(default_factory=dict)
    active: bool = True


def _words(text: str) -> set[str]:
    return {word.strip(".,!?«»\"'").casefold() for word in text.split() if word}


class FakeMemoryProvider:
    def __init__(self, *, auto_save_categories: Collection[MemoryCategory] = ()) -> None:
        self.auto_save_categories = frozenset(auto_save_categories)
        self.facts: dict[str, tuple[str, FactRecord]] = {}
        self.people: dict[str, tuple[str, FakePerson]] = {}
        self.used: list[str] = []

    def add_fact(self, user_id: str, fact: FactRecord) -> None:
        self.facts[fact.id] = (user_id, fact)

    def add_person(self, user_id: str, person: FakePerson) -> None:
        self.people[person.ref.id] = (user_id, person)

    async def context(
        self,
        *,
        user_id: str,
        query: str,
        categories: Collection[MemoryCategory] | None = None,
        limit: int = 8,
    ) -> Sequence[FactRecord]:
        query_words = _words(query)
        scored: list[tuple[int, FactRecord]] = []
        for owner, fact in self.facts.values():
            if owner != user_id or fact.status != "active":
                continue
            if categories is not None and fact.category not in categories:
                continue
            score = len(query_words & _words(fact.text))
            if score:
                scored.append((score, fact))
        scored.sort(key=lambda item: -item[0])
        return [fact for _, fact in scored[:limit]]

    async def suggest(
        self, *, user_id: str, text: str, category: MemoryCategory, source: MemorySource
    ) -> FactRecord:
        fact = FactRecord(
            id=str(uuid.uuid4()),
            text=text,
            category=category,
            status="active" if category in self.auto_save_categories else "suggested",
            sensitivity="normal",
            source=source,
        )
        self.add_fact(user_id, fact)
        return fact

    async def resolve_person(self, *, user_id: str, name: str) -> Sequence[PersonRef]:
        wanted = name.casefold()
        return [
            person.ref
            for owner, person in self.people.values()
            if owner == user_id
            and person.active
            and wanted in {person.ref.display_name.casefold(), *map(str.casefold, person.aliases)}
        ]

    async def person_identifiers(self, *, user_id: str, person_id: str) -> Mapping[str, str | int]:
        owner, person = self.people.get(person_id, ("", None))
        if person is None or owner != user_id:
            return {}
        return dict(person.identifiers)

    async def mark_used(self, *, user_id: str, record_ids: Sequence[str]) -> None:
        self.used.extend(
            record_id
            for record_id in record_ids
            if self.facts.get(record_id, ("", None))[0] == user_id
        )
