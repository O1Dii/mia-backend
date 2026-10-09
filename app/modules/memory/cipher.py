"""Per-user encryption of memory content on top of M11's envelope crypto.

``UserKeys`` is derived once per transaction from the user's DEK
(``KeyVault.ensure_dek``):

- content is sealed with AES-256-GCM under the DEK, AAD
  ``mia.secret.v1|<user>|memory.<field>|<record>`` (same binding as M11), so a
  blob copied to another row or user fails authentication;
- search terms are keyed with HMAC-SHA256 under a sub-key of the DEK; only
  these opaque tokens reach the ``tsvector`` blind index.

Destroying the DEK (crypto-shred) makes both unrecoverable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import open_sealed, seal
from app.modules.models.models import UserDataKey
from app.modules.models.vault import KeyVault, secret_aad

_INDEX_LABEL = b"mia.memory.index.v1"
TOKEN_HEX = 24


@dataclass(frozen=True, slots=True)
class UserKeys:
    user_id: uuid.UUID
    dek: bytes
    index_key: bytes

    def __repr__(self) -> str:
        return f"UserKeys(user_id={self.user_id!s})"

    def seal_text(self, value: str, *, purpose: str, record_id: uuid.UUID) -> bytes:
        return seal(
            self.dek,
            value.encode("utf-8"),
            aad=secret_aad(self.user_id, f"memory.{purpose}", record_id),
        )

    def open_text(self, blob: bytes, *, purpose: str, record_id: uuid.UUID) -> str:
        return open_sealed(
            self.dek, blob, aad=secret_aad(self.user_id, f"memory.{purpose}", record_id)
        ).decode("utf-8")

    def seal_json(self, value: Any, *, purpose: str, record_id: uuid.UUID) -> bytes:
        return self.seal_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True),
            purpose=purpose,
            record_id=record_id,
        )

    def open_json(self, blob: bytes, *, purpose: str, record_id: uuid.UUID) -> Any:
        return json.loads(self.open_text(blob, purpose=purpose, record_id=record_id))

    def tokens(self, terms: Iterable[str]) -> list[str]:
        """Blind-index tokens: ``t`` + truncated HMAC of each term (hex, parser-safe)."""
        out: dict[str, None] = {}
        for term in terms:
            digest = hmac.new(self.index_key, term.encode("utf-8"), hashlib.sha256).hexdigest()
            out.setdefault("t" + digest[:TOKEN_HEX], None)
        return list(out)


class MemoryCipher:
    def __init__(self, vault: KeyVault) -> None:
        self._vault = vault

    async def keys(self, session: AsyncSession, user_id: uuid.UUID) -> UserKeys:
        """Keys for writing: the DEK is created on first use."""
        dek = await self._vault.ensure_dek(session, user_id)
        return self._derive(user_id, dek)

    async def existing_keys(self, session: AsyncSession, user_id: uuid.UUID) -> UserKeys | None:
        """Keys for reading: ``None`` when the user has no DEK (nothing can be stored)."""
        if await session.get(UserDataKey, user_id) is None:
            return None
        return await self.keys(session, user_id)

    def _derive(self, user_id: uuid.UUID, dek: bytes) -> UserKeys:
        index_key = hmac.new(dek, _INDEX_LABEL, hashlib.sha256).digest()
        return UserKeys(user_id=user_id, dek=dek, index_key=index_key)

    async def shred(self, session: AsyncSession, user_id: uuid.UUID) -> bool:
        """Destroy the user's DEK (``KeyVault.shred_user``; also voids sealed model keys)."""
        return await self._vault.shred_user(session, user_id)
