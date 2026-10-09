"""Interactive Sign in with ChatGPT for a locally or self-hosted MIA.

The redirect URI is a loopback address of the machine running the backend, so this flow
suits the owner-hosted deployment (OI-M01-002). ``start`` opens a short-lived listener and
returns the authorize URL; a background task completes the exchange, verifies the ID token
signature via JWKS and stores the sealed tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.chatgpt_plan.credentials import PlanCredentialStore
from app.modules.models.chatgpt_plan.jwks import IdTokenError, JwksVerifier
from app.modules.models.models import GatewaySetting
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HOST_ID_KEY = "ext_agent_host_id"
SignInStatus = Literal["pending", "completed", "failed"]


async def ensure_host_id(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """Persistent non-secret ``urn:uuid`` for ``ext_agent_host_id``, created before first login."""
    async with session_factory() as session, session.begin():
        await session.execute(
            pg_insert(GatewaySetting)
            .values(key=HOST_ID_KEY, value=f"urn:uuid:{uuid.uuid4()}")
            .on_conflict_do_nothing(index_elements=["key"])
        )
        row = await session.get(GatewaySetting, HOST_ID_KEY)
        assert row is not None
        return row.value


@dataclass(slots=True)
class SignInAttempt:
    id: str
    user_id: uuid.UUID
    authorize_url: str
    expires_at: float
    status: SignInStatus = "pending"
    error_code: str | None = None
    connection_id: uuid.UUID | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class SignInCoordinator:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: PlanCredentialStore,
        client: httpx.AsyncClient,
        verifier: JwksVerifier,
        preferred_port: int = oauth.DEFAULT_PORT,
        timeout_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
        server_factory: Callable[[int], oauth.CallbackServer] = oauth.CallbackServer,
    ) -> None:
        self._sessions = session_factory
        self._store = store
        self._client = client
        self._verifier = verifier
        self._port = preferred_port
        self._timeout = timeout_seconds
        self._clock = clock
        self._server_factory = server_factory
        self._attempts: dict[str, SignInAttempt] = {}
        self._by_user: dict[uuid.UUID, str] = {}

    async def start(self, user_id: uuid.UUID) -> SignInAttempt:
        previous = self._by_user.get(user_id)
        if previous and (old := self._attempts.get(previous)) and old.task is not None:
            old.task.cancel()
        host_id = await ensure_host_id(self._sessions)
        client_id = await self._store.saved_client_id(user_id)
        verifier, challenge = oauth.pkce_pair()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        server = self._server_factory(self._port)
        server.start()
        url = oauth.build_authorize_url(
            redirect_uri=server.redirect_uri,
            state=state,
            nonce=nonce,
            challenge=challenge,
            host_id=host_id,
            client_id=client_id,
        )
        attempt = SignInAttempt(
            id=secrets.token_urlsafe(16),
            user_id=user_id,
            authorize_url=url,
            expires_at=self._clock() + self._timeout,
        )
        attempt.task = asyncio.create_task(
            self._complete(attempt, server, verifier, state, nonce), name="chatgpt-plan-sign-in"
        )
        self._attempts[attempt.id] = attempt
        self._by_user[user_id] = attempt.id
        self._evict()
        return attempt

    def status(self, user_id: uuid.UUID, attempt_id: str) -> SignInAttempt | None:
        attempt = self._attempts.get(attempt_id)
        return attempt if attempt is not None and attempt.user_id == user_id else None

    def _evict(self) -> None:
        now = self._clock()
        for key in [k for k, a in self._attempts.items() if a.expires_at + 600 < now]:
            del self._attempts[key]

    async def _complete(
        self,
        attempt: SignInAttempt,
        server: oauth.CallbackServer,
        verifier: str,
        state: str,
        nonce: str,
    ) -> None:
        try:
            try:
                params = await server.wait(self._timeout)
            finally:
                await server.stop()
            if params.get("error"):
                raise _Failed("authorization_denied")
            if not secrets.compare_digest(params.get("state", ""), state) or "code" not in params:
                raise _Failed("state_mismatch")
            client_id = params.get("client_id", "")
            if not client_id.startswith("oaiapp_"):
                raise _Failed("client_not_registered")
            try:
                tokens, id_token = await oauth.exchange_code(
                    self._client,
                    client_id=client_id,
                    code=params["code"],
                    verifier=verifier,
                    redirect_uri=server.redirect_uri,
                    clock=self._clock,
                )
            except oauth.TokenEndpointError:
                raise _Failed("token_exchange_failed") from None
            if oauth.DIRECT_SCOPE not in tokens.scopes:
                raise _Failed("scope_missing")
            try:
                await self._verifier.verify(id_token, audience=client_id, nonce=nonce)
            except IdTokenError:
                raise _Failed("id_token_invalid") from None
            attempt.connection_id = await self._store.save_sign_in(attempt.user_id, tokens)
            attempt.status = "completed"
        except TimeoutError:
            attempt.status, attempt.error_code = "failed", "timeout"
        except _Failed as failure:
            attempt.status, attempt.error_code = "failed", failure.code
        except asyncio.CancelledError:
            attempt.status, attempt.error_code = "failed", "superseded"
            raise
        except Exception:  # noqa: BLE001 - never leak provider detail; surface a stable code
            attempt.status, attempt.error_code = "failed", "internal_error"

    async def aclose(self) -> None:
        tasks = [a.task for a in self._attempts.values() if a.task and not a.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


class _Failed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
