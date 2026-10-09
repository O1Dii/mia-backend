"""Plain records of the Memory Ledger and their contract (wire) form."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast, get_args

from app.core.ports.memory_provider import (
    MemoryCategory,
    MemorySource,
    RecordStatus,
    Sensitivity,
    SourceType,
)

CATEGORIES: Final[tuple[MemoryCategory, ...]] = get_args(MemoryCategory)
SOURCE_TYPES: Final[tuple[SourceType, ...]] = get_args(SourceType)
SENSITIVE_CATEGORIES: Final[frozenset[MemoryCategory]] = frozenset({"health", "finance"})
"""Never auto-saved; reach the model only when relevant to the query."""
HistoryRetention = Literal["30d", "1y", "forever", "none"]
RETENTIONS: Final[tuple[HistoryRetention, ...]] = get_args(HistoryRetention)
FactScope = Literal["user", "automation"]

PHONE_PATTERN = re.compile(r"^\+[1-9][0-9]{6,14}$")
TELEGRAM_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")
IDENTIFIER_KEYS: Final = ("telegram_user_id", "telegram_username", "phone", "email")


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def sensitivity_for(category: MemoryCategory) -> Sensitivity:
    return "sensitive" if category in SENSITIVE_CATEGORIES else "normal"


def clean_identifiers(raw: Mapping[str, Any] | None) -> dict[str, str | int]:
    """Keep only well-formed identifiers of ``memory.schema.json#/$defs/Person``."""
    out: dict[str, str | int] = {}
    if not raw:
        return out
    telegram_id = raw.get("telegram_user_id")
    if isinstance(telegram_id, int) and not isinstance(telegram_id, bool) and telegram_id > 0:
        out["telegram_user_id"] = telegram_id
    username = raw.get("telegram_username")
    if isinstance(username, str):
        username = username.removeprefix("@")
        if TELEGRAM_USERNAME_PATTERN.match(username):
            out["telegram_username"] = username
    phone = raw.get("phone")
    if isinstance(phone, str):
        normalized = normalize_phone(phone)
        if normalized is not None:
            out["phone"] = normalized
    email = raw.get("email")
    if isinstance(email, str) and len(email) <= 254 and EMAIL_PATTERN.match(email.strip()):
        out["email"] = email.strip()
    return out


def normalize_phone(value: str) -> str | None:
    """E.164 only (``+`` and digits); local formats are not guessed."""
    stripped = value.strip()
    if not stripped.startswith("+"):
        return None
    digits = "+" + "".join(ch for ch in stripped if ch.isdigit())
    return digits if PHONE_PATTERN.match(digits) else None


@dataclass(frozen=True, slots=True)
class Fact:
    id: uuid.UUID
    text: str
    category: MemoryCategory
    status: RecordStatus
    sensitivity: Sensitivity
    scope: FactScope
    source: MemorySource
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
    person_id: uuid.UUID | None = None

    def contract(self) -> dict[str, Any]:
        return {
            "kind": "fact",
            "id": str(self.id),
            "text": self.text,
            "category": self.category,
            "source": source_contract(self.source),
            "status": self.status,
            "sensitivity": self.sensitivity,
            "scope": self.scope,
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
            "last_used_at": isoformat(self.last_used_at) if self.last_used_at else None,
        }


@dataclass(frozen=True, slots=True)
class Person:
    id: uuid.UUID
    display_name: str
    aliases: tuple[str, ...]
    relation: str | None
    identifiers: Mapping[str, str | int]
    trusted: bool
    status: RecordStatus
    sensitivity: Sensitivity
    source: MemorySource
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None

    @property
    def model_ref(self) -> str:
        return f"person:{self.id}"

    @property
    def names(self) -> list[str]:
        return [self.display_name, *self.aliases]

    def contract(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": "person",
            "id": str(self.id),
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "identifiers": dict(self.identifiers),
            "trusted": self.trusted,
            "source": source_contract(self.source),
            "status": self.status,
            "sensitivity": self.sensitivity,
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
            "last_used_at": isoformat(self.last_used_at) if self.last_used_at else None,
        }
        if self.relation:
            body["relation"] = self.relation
        return body


@dataclass(frozen=True, slots=True)
class Preference:
    id: uuid.UUID
    key: str
    value: str | int | float | bool
    source: MemorySource
    created_at: datetime
    updated_at: datetime

    def contract(self) -> dict[str, Any]:
        return {
            "kind": "preference",
            "id": str(self.id),
            "key": self.key,
            "value": self.value,
            "source": source_contract(self.source),
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class PrivacySettings:
    autosave_categories: frozenset[MemoryCategory] = frozenset()
    history_retention: HistoryRetention = "forever"

    def contract(self) -> dict[str, Any]:
        return {
            "autosave_categories": sorted(self.autosave_categories),
            "history_retention": self.history_retention,
        }


@dataclass(frozen=True, slots=True)
class PersonInput:
    """A person to remember (from the model, the API, a contact or an import)."""

    display_name: str
    relation: str | None = None
    aliases: tuple[str, ...] = ()
    identifiers: Mapping[str, str | int] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "relation": self.relation,
            "aliases": list(self.aliases),
            "identifiers": dict(self.identifiers),
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> PersonInput:
        relation = raw.get("relation")
        return cls(
            display_name=str(raw["display_name"]),
            relation=str(relation) if relation else None,
            aliases=tuple(str(a) for a in raw.get("aliases") or ()),
            identifiers=clean_identifiers(raw.get("identifiers")),
        )


@dataclass(frozen=True, slots=True)
class AccessEntry:
    at: datetime
    actor: str
    action: str
    record_count: int
    run_id: uuid.UUID | None
    app_client_id: str | None

    def contract(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "at": isoformat(self.at),
            "actor": self.actor,
            "action": self.action,
            "record_count": self.record_count,
        }
        if self.run_id is not None:
            body["run_id"] = str(self.run_id)
        if self.app_client_id is not None:
            body["app_client_id"] = self.app_client_id
        return body


def source_contract(source: MemorySource) -> dict[str, Any]:
    body: dict[str, Any] = {"type": source.type}
    if source.ref:
        body["ref"] = source.ref
    return body


def as_category(value: str) -> MemoryCategory:
    if value not in CATEGORIES:
        raise ValueError("unknown category")
    return cast(MemoryCategory, value)
