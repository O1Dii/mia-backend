"""Connection store and service: CRUD with sealed secrets, resolution for the gateway.

The API surface never returns a key: ``ConnectionView`` carries ``has_key`` and the last
four characters only (and only for keys long enough that four characters reveal nothing).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import CryptoError
from app.core.ports.model_gateway import ModelGatewayError
from app.core.ports.types import DEFAULT_MODEL
from app.modules.models.catalog import (
    ConnectionValidationError,
    ProviderSpec,
    provider_spec,
    resolve_base_url,
    validate_capability,
)
from app.modules.models.models import ModelConnection
from app.modules.models.vault import KeyVault

PURPOSE_API_KEY = "api_key"
PURPOSE_PLAN_TOKENS = "plan_tokens"
MIN_KEY_LEN_FOR_LAST4 = 12
MAX_CONNECTIONS_PER_USER = 20


class ConnectionNotFoundError(Exception):
    pass


class ConnectionConflictError(Exception):
    """A uniqueness rule failed, e.g. a second ChatGPT plan connection."""


@dataclass(frozen=True, slots=True)
class ConnectionView:
    """Public representation: contains no secret material."""

    id: uuid.UUID
    provider: str
    label: str
    model: str
    base_url: str | None
    capability: str
    has_key: bool
    key_last4: str | None
    status: str
    paused_until: datetime | None
    is_primary: bool
    verified: bool
    last_tested_at: datetime | None
    last_test_ok: bool | None
    last_test_error_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedConnection:
    """A connection ready for the gateway; the key is excluded from ``repr``."""

    id: uuid.UUID
    user_id: uuid.UUID
    spec: ProviderSpec
    model: str
    base_url: str
    capability: str
    status: str
    paused_until: datetime | None
    is_primary: bool
    api_key: SecretStr | None


def to_view(row: ModelConnection) -> ConnectionView:
    spec = provider_spec(row.provider)
    return ConnectionView(
        id=row.id,
        provider=row.provider,
        label=row.label,
        model=row.model,
        base_url=row.base_url,
        capability=row.capability,
        has_key=row.secret_ciphertext is not None,
        key_last4=row.key_last4,
        status=row.status,
        paused_until=row.paused_until,
        is_primary=row.is_primary,
        verified=spec.verified and row.capability == "tools",
        last_tested_at=row.last_tested_at,
        last_test_ok=row.last_test_ok,
        last_test_error_code=row.last_test_error_code,
        created_at=row.created_at,
    )


def _last4(key: str) -> str | None:
    return key[-4:] if len(key) >= MIN_KEY_LEN_FOR_LAST4 else None


def _is_available(row: ModelConnection, now: datetime) -> bool:
    if row.status == "needs_reauth":
        return False
    return not (row.status == "paused" and row.paused_until is not None and row.paused_until > now)


class ConnectionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        vault: KeyVault,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = session_factory
        self._vault = vault
        self._clock = clock

    # ---------------------------------------------------------------- CRUD

    async def list_connections(self, user_id: uuid.UUID) -> list[ConnectionView]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ModelConnection)
                .where(ModelConnection.user_id == user_id)
                .order_by(ModelConnection.created_at, ModelConnection.id)
            )
            return [to_view(r) for r in rows]

    async def get(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> ConnectionView:
        async with self._sessions() as session:
            return to_view(await self._row(session, user_id, connection_id))

    async def create(
        self,
        user_id: uuid.UUID,
        *,
        provider: str,
        label: str,
        model: str,
        api_key: str | None,
        base_url: str | None = None,
        capability: str | None = None,
        make_primary: bool | None = None,
    ) -> ConnectionView:
        spec = provider_spec(provider)
        if spec.id == "chatgpt-plan":
            raise ConnectionValidationError("Use the ChatGPT sign-in flow for this provider.")
        key = (api_key or "").strip()
        if spec.needs_key and not key:
            raise ConnectionValidationError("This provider requires an API key.")
        resolved_url = resolve_base_url(spec, base_url)
        cap = validate_capability(spec, capability)
        connection_id = uuid.uuid4()
        async with self._sessions() as session, session.begin():
            count = len(
                (
                    await session.scalars(
                        select(ModelConnection.id).where(ModelConnection.user_id == user_id)
                    )
                ).all()
            )
            if count >= MAX_CONNECTIONS_PER_USER:
                raise ConnectionValidationError("Too many connections.")
            has_primary = await self._has_primary(session, user_id)
            sealed = (
                await self._vault.seal_secret(
                    session,
                    user_id,
                    key.encode(),
                    purpose=PURPOSE_API_KEY,
                    record_id=connection_id,
                )
                if key
                else None
            )
            row = ModelConnection(
                id=connection_id,
                user_id=user_id,
                provider=spec.id,
                label=label.strip(),
                model=model.strip(),
                base_url=resolved_url if spec.local else None,
                capability=cap,
                secret_ciphertext=sealed,
                key_last4=_last4(key) if key else None,
                is_primary=False,
            )
            session.add(row)
            await session.flush()
            if cap == "tools" and (make_primary or (make_primary is None and not has_primary)):
                await self._set_primary(session, user_id, row)
            await session.refresh(row)
            return to_view(row)

    async def update(
        self,
        user_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        label: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        capability: str | None = None,
        make_primary: bool | None = None,
    ) -> ConnectionView:
        async with self._sessions() as session, session.begin():
            row = await self._row(session, user_id, connection_id, lock=True)
            spec = provider_spec(row.provider)
            if label is not None:
                row.label = label.strip()
            if model is not None:
                row.model = model.strip()
            if base_url is not None:
                resolved_url = resolve_base_url(spec, base_url)  # rejects cloud overrides
                row.base_url = resolved_url if spec.local else None
            if capability is not None:
                row.capability = validate_capability(spec, capability)
                if row.capability == "chat_only":
                    row.is_primary = False
            if api_key is not None:
                if spec.id == "chatgpt-plan":
                    raise ConnectionValidationError("This connection has no API key.")
                key = api_key.strip()
                if not key:
                    raise ConnectionValidationError("The API key must not be empty.")
                row.secret_ciphertext = await self._vault.seal_secret(
                    session,
                    user_id,
                    key.encode(),
                    purpose=PURPOSE_API_KEY,
                    record_id=row.id,
                )
                row.key_last4 = _last4(key)
                if row.status == "needs_reauth":
                    row.status = "active"
            if make_primary is True:
                if row.capability != "tools":
                    raise ConnectionValidationError("A chat-only connection cannot be primary.")
                await self._set_primary(session, user_id, row)
            elif make_primary is False:
                row.is_primary = False
            row.updated_at = self._clock()
            await session.flush()
            return to_view(row)

    async def delete(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        async with self._sessions() as session, session.begin():
            row = await self._row(session, user_id, connection_id, lock=True)
            await session.delete(row)

    # ----------------------------------------------------------- resolution

    async def resolve(
        self, user_id: uuid.UUID, model: str, *, include_unavailable: bool = False
    ) -> list[ResolvedConnection]:
        """Ordered candidates for a model reference.

        ``mia:default`` -> primary first, then the other tools-capable connections (the
        fallback chain); ``<provider>/<model>`` -> exactly the matching connections.
        """
        now = self._clock()
        async with self._sessions() as session:
            rows = list(
                await session.scalars(
                    select(ModelConnection)
                    .where(ModelConnection.user_id == user_id)
                    .order_by(ModelConnection.created_at, ModelConnection.id)
                )
            )
            if model == DEFAULT_MODEL:
                candidates = [r for r in rows if r.capability == "tools"]
                candidates.sort(
                    key=lambda r: (not r.is_primary, r.provider != "chatgpt-plan", r.created_at)
                )
            else:
                provider, separator, name = model.partition("/")
                if not separator or not name:
                    raise ModelGatewayError(
                        "invalid_request", "Use 'mia:default' or '<provider>/<model>'."
                    )
                candidates = [r for r in rows if r.provider == provider and r.model == name]
            if not include_unavailable:
                candidates = [r for r in candidates if _is_available(r, now)]
            resolved: list[ResolvedConnection] = []
            for row in candidates:
                try:
                    resolved.append(await self._resolved(session, row))
                except CryptoError:
                    continue  # unreadable secret (shredded key, corruption): not usable
            return resolved

    async def resolve_one(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> ResolvedConnection:
        async with self._sessions() as session:
            return await self._resolved(session, await self._row(session, user_id, connection_id))

    async def _resolved(self, session: AsyncSession, row: ModelConnection) -> ResolvedConnection:
        spec = provider_spec(row.provider)
        key: SecretStr | None = None
        if row.secret_ciphertext is not None and spec.id != "chatgpt-plan":
            plain = await self._vault.open_secret(
                session,
                row.user_id,
                row.secret_ciphertext,
                purpose=PURPOSE_API_KEY,
                record_id=row.id,
            )
            key = SecretStr(plain.decode())
        return ResolvedConnection(
            id=row.id,
            user_id=row.user_id,
            spec=spec,
            model=row.model,
            base_url=row.base_url or spec.default_url,
            capability=row.capability,
            status=row.status,
            paused_until=row.paused_until,
            is_primary=row.is_primary,
            api_key=key,
        )

    # ------------------------------------------------------------ state

    async def pause(self, connection_id: uuid.UUID, *, seconds: int, reason: str) -> None:
        until = self._clock() + timedelta(seconds=seconds)
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(ModelConnection)
                .where(ModelConnection.id == connection_id, ModelConnection.status == "active")
                .values(status="paused", paused_until=until, pause_reason=reason[:64])
            )

    async def mark_needs_reauth(self, connection_id: uuid.UUID) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(ModelConnection)
                .where(ModelConnection.id == connection_id)
                .values(status="needs_reauth")
            )

    async def record_test(
        self, connection_id: uuid.UUID, *, ok: bool, error_code: str | None
    ) -> None:
        async with self._sessions() as session, session.begin():
            values: dict[str, object] = {
                "last_tested_at": self._clock(),
                "last_test_ok": ok,
                "last_test_error_code": error_code,
            }
            if ok:
                values.update(status="active", paused_until=None, pause_reason=None)
            await session.execute(
                update(ModelConnection).where(ModelConnection.id == connection_id).values(**values)
            )

    async def resume(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> ConnectionView:
        async with self._sessions() as session, session.begin():
            row = await self._row(session, user_id, connection_id, lock=True)
            if row.status == "paused":
                row.status, row.paused_until, row.pause_reason = "active", None, None
            return to_view(row)

    # --------------------------------------------------------- internals

    @staticmethod
    async def _row(
        session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, *, lock: bool = False
    ) -> ModelConnection:
        statement = select(ModelConnection).where(
            ModelConnection.id == connection_id, ModelConnection.user_id == user_id
        )
        if lock:
            statement = statement.with_for_update()
        row = await session.scalar(statement)
        if row is None:
            raise ConnectionNotFoundError
        return row

    @staticmethod
    async def _has_primary(session: AsyncSession, user_id: uuid.UUID) -> bool:
        found = await session.scalar(
            select(ModelConnection.id).where(
                ModelConnection.user_id == user_id, ModelConnection.is_primary.is_(True)
            )
        )
        return found is not None

    @staticmethod
    async def _set_primary(session: AsyncSession, user_id: uuid.UUID, row: ModelConnection) -> None:
        await session.execute(
            update(ModelConnection)
            .where(
                ModelConnection.user_id == user_id,
                ModelConnection.is_primary.is_(True),
                ModelConnection.id != row.id,
            )
            .values(is_primary=False)
        )
        row.is_primary = True
        try:
            await session.flush()
        except IntegrityError:
            raise ConnectionConflictError from None


__all__ = [
    "ConnectionConflictError",
    "ConnectionNotFoundError",
    "ConnectionService",
    "ConnectionView",
    "ResolvedConnection",
    "to_view",
]
