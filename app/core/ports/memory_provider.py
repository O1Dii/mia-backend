"""MemoryProvider port: Memory Ledger access for runs (Review §7.3, §7.5, ADR-0012).

Schema: ``contracts/mvp/v1/memory.schema.json``. M17 implements it on top of
per-user DEK encryption; callers never see ciphertext or keys.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

MemoryCategory = Literal["profile", "work", "home", "health", "finance", "interests", "other"]
RecordStatus = Literal["suggested", "active", "rejected"]
Sensitivity = Literal["normal", "sensitive"]
SourceType = Literal["user_said", "turn", "plugin", "import", "app", "device_contacts", "telegram"]


@dataclass(frozen=True, slots=True)
class MemorySource:
    type: SourceType
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class FactRecord:
    id: str
    text: str
    category: MemoryCategory
    status: RecordStatus
    sensitivity: Sensitivity
    source: MemorySource


@dataclass(frozen=True, slots=True)
class PersonRef:
    """What the model may see about a person: never phone, email or messenger ids."""

    id: str
    display_name: str
    relation: str | None = None

    @property
    def model_ref(self) -> str:
        return f"person:{self.id}"


@runtime_checkable
class MemoryProvider(Protocol):
    async def context(
        self,
        *,
        user_id: str,
        query: str,
        categories: Collection[MemoryCategory] | None = None,
        limit: int = 8,
    ) -> Sequence[FactRecord]:
        """Up to ``limit`` relevant ``active`` facts for the model context."""
        ...

    async def suggest(
        self, *, user_id: str, text: str, category: MemoryCategory, source: MemorySource
    ) -> FactRecord:
        """``memory.remember``/``memory.suggest``: store a fact as ``suggested``
        (or ``active`` only when the user enabled auto-save for a non-sensitive
        category)."""
        ...

    async def resolve_person(self, *, user_id: str, name: str) -> Sequence[PersonRef]:
        """Match a spoken name against active people and their aliases."""
        ...

    async def person_identifiers(self, *, user_id: str, person_id: str) -> Mapping[str, str | int]:
        """Identifiers substituted by the core at execution time (e.g.
        ``telegram_user_id``). Never passed to the model or logs."""
        ...

    async def mark_used(self, *, user_id: str, record_ids: Sequence[str]) -> None:
        """Update ``last_used_at`` of records that went into a model context."""
        ...
