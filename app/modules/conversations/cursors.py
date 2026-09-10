"""Opaque, versioned keyset cursor primitives."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class InvalidCursorError(ValueError):
    """Raised when a client cursor is malformed or belongs to another feed."""


def _encode(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(cursor: str) -> dict[str, Any]:
    if not cursor or len(cursor) > 512:
        raise InvalidCursorError("cursor length is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded)
    except (UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as error:
        raise InvalidCursorError("cursor is malformed") from error
    if not isinstance(payload, dict):
        raise InvalidCursorError("cursor payload is invalid")
    return payload


@dataclass(frozen=True, slots=True)
class ConversationCursor:
    updated_at: datetime
    conversation_id: uuid.UUID

    def encode(self) -> str:
        value = self.updated_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return _encode(
            {
                "id": str(self.conversation_id),
                "kind": "conversations",
                "updated_at": value.astimezone(UTC).isoformat(),
                "v": 1,
            }
        )

    @classmethod
    def decode(cls, cursor: str) -> ConversationCursor:
        payload = _decode(cursor)
        if set(payload) != {"id", "kind", "updated_at", "v"}:
            raise InvalidCursorError("conversation cursor shape is invalid")
        if payload["kind"] != "conversations" or payload["v"] != 1:
            raise InvalidCursorError("conversation cursor version is unsupported")
        try:
            updated_at = datetime.fromisoformat(str(payload["updated_at"]))
            conversation_id = uuid.UUID(str(payload["id"]))
        except (TypeError, ValueError) as error:
            raise InvalidCursorError("conversation cursor values are invalid") from error
        if updated_at.tzinfo is None:
            raise InvalidCursorError("conversation cursor timestamp requires a timezone")
        return cls(updated_at=updated_at, conversation_id=conversation_id)


@dataclass(frozen=True, slots=True)
class EntryCursor:
    sequence: int

    def encode(self) -> str:
        if self.sequence < 1:
            raise InvalidCursorError("entry cursor sequence is invalid")
        return _encode({"kind": "entries", "sequence": self.sequence, "v": 1})

    @classmethod
    def decode(cls, cursor: str) -> EntryCursor:
        payload = _decode(cursor)
        if set(payload) != {"kind", "sequence", "v"}:
            raise InvalidCursorError("entry cursor shape is invalid")
        if payload["kind"] != "entries" or payload["v"] != 1:
            raise InvalidCursorError("entry cursor version is unsupported")
        sequence = payload["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise InvalidCursorError("entry cursor sequence is invalid")
        return cls(sequence=sequence)
