"""Encrypted storage and serialized refresh of ChatGPT plan tokens.

Refresh tokens rotate: two concurrent refreshes would burn the token and force a new
sign-in. Refresh is therefore serialized twice: an in-process lock per connection and a
``SELECT ... FOR UPDATE`` row lock (cross-process). The new access *and* refresh tokens
are written in the same transaction as the lock.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

import httpx
from app.modules.models.catalog import PLAN_DEFAULT_MODEL
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.connections import PURPOSE_PLAN_TOKENS
from app.modules.models.errors import ProviderError
from app.modules.models.models import ModelConnection
from app.modules.models.vault import KeyVault
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class PlanCredentialStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        vault: KeyVault,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sessions = session_factory
        self._vault = vault
        self._client = client
        self._clock = clock
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    def token_source(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> PlanTokenSource:
        return PlanTokenSource(self, user_id, connection_id)

    def _lock(self, connection_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(connection_id, asyncio.Lock())

    async def saved_client_id(self, user_id: uuid.UUID) -> str | None:
        """The issued ``oaiapp_`` id of an earlier sign-in (kept after sign-out/reauth)."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(ModelConnection).where(
                    ModelConnection.user_id == user_id, ModelConnection.provider == "chatgpt-plan"
                )
            )
            if row is None or row.secret_ciphertext is None:
                return None
            return (await self._open(session, row)).client_id

    async def _open(self, session: AsyncSession, row: ModelConnection) -> oauth.TokenSet:
        assert row.secret_ciphertext is not None
        plain = await self._vault.open_secret(
            session,
            row.user_id,
            row.secret_ciphertext,
            purpose=PURPOSE_PLAN_TOKENS,
            record_id=row.id,
        )
        return oauth.TokenSet.loads(plain)

    async def _seal(
        self, session: AsyncSession, row: ModelConnection, tokens: oauth.TokenSet
    ) -> None:
        row.secret_ciphertext = await self._vault.seal_secret(
            session, row.user_id, tokens.dumps(), purpose=PURPOSE_PLAN_TOKENS, record_id=row.id
        )

    async def save_sign_in(
        self, user_id: uuid.UUID, tokens: oauth.TokenSet, *, model: str = PLAN_DEFAULT_MODEL
    ) -> uuid.UUID:
        """Create or re-activate the user's single plan connection with fresh tokens."""
        async with self._sessions() as session, session.begin():
            row = await session.scalar(
                select(ModelConnection)
                .where(
                    ModelConnection.user_id == user_id, ModelConnection.provider == "chatgpt-plan"
                )
                .with_for_update()
            )
            if row is None:
                row = ModelConnection(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    provider="chatgpt-plan",
                    label="ChatGPT plan",
                    model=model,
                    capability="tools",
                )
                session.add(row)
                has_primary = await session.scalar(
                    select(ModelConnection.id).where(
                        ModelConnection.user_id == user_id, ModelConnection.is_primary.is_(True)
                    )
                )
                row.is_primary = has_primary is None
                await session.flush()
            await self._seal(session, row, tokens)
            row.status, row.paused_until, row.pause_reason = "active", None, None
            return row.id

    async def access_token(
        self, user_id: uuid.UUID, connection_id: uuid.UUID, *, rejected: str | None = None
    ) -> str:
        """A valid access token, refreshing (serialized) when near expiry or ``rejected``."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(ModelConnection).where(
                    ModelConnection.id == connection_id, ModelConnection.user_id == user_id
                )
            )
            if row is None or row.secret_ciphertext is None or row.status == "needs_reauth":
                raise ProviderError("model_not_configured", needs_reauth=True)
            tokens = await self._open(session, row)
        if tokens.fresh(self._clock()) and tokens.access_token != rejected:
            return tokens.access_token
        return await self._refresh(user_id, connection_id, rejected)

    async def _refresh(
        self, user_id: uuid.UUID, connection_id: uuid.UUID, rejected: str | None
    ) -> str:
        failure: ProviderError | None = None
        token = ""
        async with self._lock(connection_id):
            async with self._sessions() as session, session.begin():
                row = await session.scalar(
                    select(ModelConnection)
                    .where(ModelConnection.id == connection_id, ModelConnection.user_id == user_id)
                    .with_for_update()
                )
                if row is None or row.secret_ciphertext is None:
                    raise ProviderError("model_not_configured", needs_reauth=True)
                tokens = await self._open(session, row)
                if tokens.fresh(self._clock()) and tokens.access_token != rejected:
                    return tokens.access_token  # another worker refreshed while we waited
                if not tokens.signed_in:
                    row.status = "needs_reauth"
                    failure = ProviderError("model_not_configured", needs_reauth=True)
                else:
                    try:
                        renewed = await oauth.refresh_tokens(self._client, tokens, self._clock)
                    except oauth.TokenEndpointError as exc:
                        if exc.permanent:
                            await self._seal(session, row, tokens.signed_out())
                            row.status = "needs_reauth"
                            failure = ProviderError(
                                "model_not_configured",
                                provider_code=exc.code,
                                needs_reauth=True,
                            )
                        else:
                            failure = ProviderError(
                                "provider_unavailable", retryable=True, provider_code=exc.code
                            )
                    else:
                        await self._seal(session, row, renewed)
                        token = renewed.access_token
        if failure is not None:
            raise failure
        return token

    async def sign_out(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> bool:
        """Revoke the refresh token at the issuer and drop local tokens; keeps ``client_id``."""
        async with self._lock(connection_id):
            async with self._sessions() as session, session.begin():
                row = await session.scalar(
                    select(ModelConnection)
                    .where(ModelConnection.id == connection_id, ModelConnection.user_id == user_id)
                    .with_for_update()
                )
                if row is None or row.secret_ciphertext is None:
                    return False
                tokens = await self._open(session, row)
                revoked = await oauth.revoke_refresh_token(self._client, tokens)
                await self._seal(session, row, tokens.signed_out())
                row.status = "needs_reauth"
                return revoked


class PlanTokenSource:
    """``TokenSource`` for one plan connection; remembers the token it handed out."""

    def __init__(
        self, store: PlanCredentialStore, user_id: uuid.UUID, connection_id: uuid.UUID
    ) -> None:
        self._store = store
        self._user_id = user_id
        self._connection_id = connection_id
        self._last: str | None = None
        self._rejected: str | None = None

    async def access_token(self) -> str:
        self._last = await self._store.access_token(
            self._user_id, self._connection_id, rejected=self._rejected
        )
        return self._last

    async def invalidate(self) -> None:
        self._rejected = self._last
