"""Per-user envelope encryption on top of ``app.core.crypto``.

``KeyVault`` creates, unwraps, rotates and destroys per-user DEKs. Sealed secrets are
bound by AAD to ``(user, purpose, record)`` so a blob cannot be moved between users or
rows. Destroying the wrapped DEK (``shred_user``) makes every sealed secret of that user
permanently unreadable, which is the crypto-shred required for account deletion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, MasterKeyring, new_dek, open_sealed, seal
from app.modules.models.models import ModelConnection, UserDataKey


class DekUnavailableError(CryptoError):
    """The user's DEK does not exist (never created or crypto-shredded)."""


def _dek_context(user_id: uuid.UUID) -> bytes:
    return f"mia.dek.v1|{user_id}".encode()


def secret_aad(user_id: uuid.UUID, purpose: str, record_id: uuid.UUID) -> bytes:
    return f"mia.secret.v1|{user_id}|{purpose}|{record_id}".encode()


class KeyVault:
    def __init__(self, keyring: MasterKeyring) -> None:
        self._keyring = keyring

    @property
    def keyring(self) -> MasterKeyring:
        return self._keyring

    async def _load_dek(self, session: AsyncSession, user_id: uuid.UUID) -> bytes | None:
        row = await session.get(UserDataKey, user_id)
        if row is None:
            return None
        return self._keyring.unwrap(
            row.master_key_id, row.wrapped_dek, context=_dek_context(user_id)
        )

    async def ensure_dek(self, session: AsyncSession, user_id: uuid.UUID) -> bytes:
        """Return the user's DEK, creating it on first use (race-safe)."""
        existing = await self._load_dek(session, user_id)
        if existing is not None:
            return existing
        dek = new_dek()
        key_id, blob = self._keyring.wrap(dek, context=_dek_context(user_id))
        await session.execute(
            pg_insert(UserDataKey)
            .values(user_id=user_id, wrapped_dek=blob, master_key_id=key_id)
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        winner = await self._load_dek(session, user_id)
        if winner is None:  # pragma: no cover - the insert above guarantees a row
            raise DekUnavailableError("The data key could not be created.")
        return winner

    async def seal_secret(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        plaintext: bytes,
        *,
        purpose: str,
        record_id: uuid.UUID,
    ) -> bytes:
        dek = await self.ensure_dek(session, user_id)
        return seal(dek, plaintext, aad=secret_aad(user_id, purpose, record_id))

    async def open_secret(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        blob: bytes,
        *,
        purpose: str,
        record_id: uuid.UUID,
    ) -> bytes:
        dek = await self._load_dek(session, user_id)
        if dek is None:
            raise DekUnavailableError("The data key for this user is gone.")
        return open_sealed(dek, blob, aad=secret_aad(user_id, purpose, record_id))

    async def rotate_master_key(self, session: AsyncSession) -> int:
        """Re-wrap every DEK that is not under the active master key; returns the count."""
        rows = (
            await session.scalars(
                select(UserDataKey).where(UserDataKey.master_key_id != self._keyring.active_id)
            )
        ).all()
        for row in rows:
            row.master_key_id, row.wrapped_dek = self._keyring.rewrap(
                row.master_key_id, row.wrapped_dek, context=_dek_context(row.user_id)
            )
            row.rewrapped_at = datetime.now(UTC)
        await session.flush()
        return len(rows)

    async def shred_user(self, session: AsyncSession, user_id: uuid.UUID) -> bool:
        """Destroy the user's DEK and scrub their sealed connection secrets.

        Returns True when a DEK existed. The connection rows stay (so the user sees that
        a reconnect is needed) but hold no ciphertext and are marked ``needs_reauth``.
        """
        await session.execute(
            update(ModelConnection)
            .where(ModelConnection.user_id == user_id)
            .values(secret_ciphertext=None, key_last4=None, status="needs_reauth")
        )
        result = await session.execute(delete(UserDataKey).where(UserDataKey.user_id == user_id))
        return bool(result.rowcount)
