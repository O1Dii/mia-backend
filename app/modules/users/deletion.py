"""Account deletion (M17-R1, ``DELETE /v1/account``; OI-M17-004, OI-M15-006).

The order matters:

1. the user's live runs are cancelled (they would write into rows that are about to vanish);
2. plugin accounts are revoked at their providers and their tokens deleted — the tokens are
   sealed with the user's data key, so this happens before the key is destroyed;
3. memory is erased and the data key crypto-shredded (``MemoryService.shred_account``):
   whatever ciphertext survives elsewhere (backups) becomes unreadable;
4. one transaction locks the ``users`` row, writes the journal entry and deletes the row.
   Everything else cascades with it: Android refresh families and devices, web and WebView
   cookie sessions, Mia sign-in grants and tokens («Войти через Mia»), push tokens, the
   Telegram link, chats, runs, jobs, inbox, installations, model connections.

Steps 1–3 can be repeated: a failure before step 4 leaves a working account that the user
deletes again. After step 4 the old bearer and cookie answer 401 and a new Google sign-in
creates a new, empty account (the account key is the Google ``sub``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.users.models import AccountDeletion, User

logger = logging.getLogger(__name__)

RequestedVia = Literal["bearer", "cookie"]


class ProviderRevoker(Protocol):
    """Plugin connections (``ConnectionService``): revoke upstream, delete the tokens."""

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int: ...


class MemoryShredder(Protocol):
    """``MemoryService``: erase memory rows and destroy the data key."""

    async def shred_account(self, *, user_id: str) -> bool: ...


RunCanceller = Callable[[uuid.UUID], Awaitable[int]]
"""Cancels the user's unfinished runs and waits briefly for them; returns how many there were."""


class AccountDeletionIncompleteError(RuntimeError):
    """A step failed; the account still exists (or is half-erased) and deletion may be retried."""


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    deleted: bool
    """False when the account was already gone (a concurrent or repeated request)."""
    provider_connections: int = 0
    memory_shredded: bool = False
    runs_cancelled: int = 0


class AccountDeletionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        memory: MemoryShredder | None = None,
        providers: ProviderRevoker | None = None,
        cancel_runs: RunCanceller | None = None,
    ) -> None:
        self._sessions = session_factory
        self._memory = memory
        self._providers = providers
        self._cancel_runs = cancel_runs

    async def delete(self, user_id: uuid.UUID, *, requested_via: RequestedVia) -> DeletionOutcome:
        if not await self._exists(user_id):
            return DeletionOutcome(deleted=False)
        runs = 0
        if self._cancel_runs is not None:
            try:
                runs = await self._cancel_runs(user_id)
            except SQLAlchemyError:
                # Not fatal: the runs' rows go with the account, their tasks stop on their own.
                logger.warning("account_deletion_runs_not_cancelled")
        try:
            providers = (
                await self._providers.revoke_all_for_user(user_id)
                if self._providers is not None
                else 0
            )
            shredded = (
                await self._memory.shred_account(user_id=str(user_id))
                if self._memory is not None
                else False
            )
            deleted = await self._delete_row(
                user_id,
                requested_via=requested_via,
                provider_connections=providers,
                memory_shredded=shredded,
            )
        except SQLAlchemyError as exc:
            logger.warning("account_deletion_incomplete", extra={"error_type": type(exc).__name__})
            raise AccountDeletionIncompleteError("account deletion did not complete") from exc
        outcome = DeletionOutcome(
            deleted=deleted,
            provider_connections=providers,
            memory_shredded=shredded,
            runs_cancelled=runs,
        )
        if deleted:
            logger.info(
                "account_deleted",
                extra={
                    "requested_via": requested_via,
                    "provider_connections": providers,
                    "memory_shredded": shredded,
                    "runs_cancelled": runs,
                },
            )
        return outcome

    async def _exists(self, user_id: uuid.UUID) -> bool:
        try:
            async with self._sessions() as session:
                return (await session.scalar(select(User.id).where(User.id == user_id))) is not None
        except SQLAlchemyError as exc:
            raise AccountDeletionIncompleteError("account lookup failed") from exc

    async def _delete_row(
        self,
        user_id: uuid.UUID,
        *,
        requested_via: RequestedVia,
        provider_connections: int,
        memory_shredded: bool,
    ) -> bool:
        async with self._sessions() as session, session.begin():
            locked = await session.scalar(
                select(User.id).where(User.id == user_id).with_for_update()
            )
            if locked is None:
                return False  # a concurrent request finished first
            session.add(
                AccountDeletion(
                    user_id=user_id,
                    requested_via=requested_via,
                    provider_connections=provider_connections,
                    memory_shredded=memory_shredded,
                )
            )
            await session.execute(delete(User).where(User.id == user_id))
        return True
