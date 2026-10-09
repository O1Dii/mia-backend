"""Interactive Sign in with ChatGPT for a locally or self-hosted MIA.

The redirect URI is always a loopback address (``http://127.0.0.1:<port>/auth/callback``, the
only kind OpenAI accepts for this public client). Two ways bring the redirect back:

* **listener** (backend on the user's own machine): ``start`` opens a short-lived listener on
  ``127.0.0.1``; the browser lands on it and the attempt completes by itself;
* **pasted address** (hosted backend, M62-T1): the browser lands on an address that does not
  open on the user's device; the user copies it from the address bar into Mia and
  :meth:`SignInCoordinator.submit_callback` validates ``state`` and completes the same attempt
  with the same PKCE verifier. OpenAI's agent sign-in has no device-code grant (checked
  2026-10-10 against developers.openai.com/siwc), so this is the hosted path.

Either way a background task exchanges the code, verifies the ID token signature via JWKS and
stores the sealed tokens.
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
from urllib.parse import parse_qs, urlsplit

import httpx
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.chatgpt_plan.credentials import PlanCredentialStore
from app.modules.models.chatgpt_plan.jwks import IdTokenError, JwksVerifier
from app.modules.models.models import GatewaySetting
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HOST_ID_KEY = "ext_agent_host_id"
SignInStatus = Literal["pending", "completed", "failed"]
MAX_PASTE_FAILURES = 5
"""Wrong pasted addresses an attempt tolerates before it fails (``state`` is 192 random bits)."""
PASTE_SETTLE_SECONDS = 30.0
MAX_PASTE_CHARS = 4096


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
    redirect_uri: str = oauth.FIXED_REDIRECT_URI
    listening: bool = False
    """True while a loopback listener on this backend waits for the redirect."""
    paste_failures: int = 0
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    server: oauth.CallbackServer | None = field(default=None, repr=False)
    state: str = field(default="", repr=False)
    callback: asyncio.Future[dict[str, str]] | None = field(default=None, repr=False)


class CallbackRejectedError(Exception):
    """A pasted address that does not belong to the attempt; the attempt stays pending."""


class AttemptNotPendingError(Exception):
    """The attempt already received its redirect, finished or expired."""


def parse_callback(raw: str) -> dict[str, str]:
    """Query parameters of a pasted redirect address (or of its bare ``?query``).

    Only the loopback callback is accepted, so a pasted link to anywhere else is refused before
    its parameters are looked at.
    """
    text = raw.strip()
    if not text or len(text) > MAX_PASTE_CHARS or any(c in text for c in "\r\n\t "):
        raise CallbackRejectedError("empty or malformed")
    if text.startswith("?"):
        query = text[1:]
    else:
        parts = urlsplit(text if "://" in text else "http://" + text)
        if (
            parts.scheme != "http"
            or parts.hostname not in {"127.0.0.1", "localhost"}
            or parts.path != oauth.CALLBACK_PATH
        ):
            raise CallbackRejectedError("not the loopback callback")
        query = parts.query
    params = {k: v[0] for k, v in parse_qs(query, max_num_fields=16).items() if v}
    if "state" not in params or not ("code" in params or "error" in params):
        raise CallbackRejectedError("no state or code")
    return params


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
        listen: bool = True,
    ) -> None:
        self._sessions = session_factory
        self._store = store
        self._client = client
        self._verifier = verifier
        self._port = preferred_port
        self._timeout = timeout_seconds
        self._clock = clock
        self._server_factory = server_factory
        self._listen = listen
        self._attempts: dict[str, SignInAttempt] = {}
        self._by_user: dict[uuid.UUID, str] = {}

    async def start(self, user_id: uuid.UUID) -> SignInAttempt:
        previous = self._by_user.get(user_id)
        if previous and (old := self._attempts.get(previous)) is not None:
            await self._cancel(old, "superseded")
        host_id = await ensure_host_id(self._sessions)
        client_id = await self._store.saved_client_id(user_id)
        verifier, challenge = oauth.pkce_pair()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        server: oauth.CallbackServer | None = None
        redirect_uri = oauth.FIXED_REDIRECT_URI
        if self._listen:
            server = self._server_factory(self._port)
            server.start()
            redirect_uri = server.redirect_uri
        url = oauth.build_authorize_url(
            redirect_uri=redirect_uri,
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
            redirect_uri=redirect_uri,
            listening=server is not None,
            state=state,
            callback=asyncio.get_running_loop().create_future(),
        )
        attempt.server = server
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

    async def submit_callback(self, attempt: SignInAttempt, raw: str) -> SignInAttempt:
        """Complete ``attempt`` with the redirect address the user pasted; wait for the result.

        A foreign or mistyped address leaves the attempt pending (up to
        :data:`MAX_PASTE_FAILURES`); the matching one is consumed exactly once.
        """
        future = attempt.callback
        if attempt.status != "pending" or future is None or future.done():
            raise AttemptNotPendingError(attempt.id)
        try:
            params = parse_callback(raw)
            if not secrets.compare_digest(params["state"], attempt.state):
                raise CallbackRejectedError("state does not match")
        except CallbackRejectedError:
            attempt.paste_failures += 1
            if attempt.paste_failures >= MAX_PASTE_FAILURES:
                await self._cancel(attempt, "too_many_attempts")
            raise
        future.set_result(params)
        if attempt.task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(attempt.task), PASTE_SETTLE_SECONDS)
        return attempt

    def _evict(self) -> None:
        now = self._clock()
        for key in [k for k, a in self._attempts.items() if a.expires_at + 600 < now]:
            del self._attempts[key]

    async def _complete(
        self,
        attempt: SignInAttempt,
        server: oauth.CallbackServer | None,
        verifier: str,
        state: str,
        nonce: str,
    ) -> None:
        try:
            try:
                params = await self._redirect(attempt, server)
            finally:
                if server is not None:
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
                    redirect_uri=attempt.redirect_uri,
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
            if attempt.status == "pending":
                attempt.status, attempt.error_code = "failed", "cancelled"
            raise
        except Exception:  # noqa: BLE001 - never leak provider detail; surface a stable code
            attempt.status, attempt.error_code = "failed", "internal_error"

    async def _redirect(
        self, attempt: SignInAttempt, server: oauth.CallbackServer | None
    ) -> dict[str, str]:
        """The first of: the listener's request, the pasted address; ``TimeoutError`` after."""
        assert attempt.callback is not None
        waiters: list[asyncio.Future[dict[str, str]]] = [attempt.callback]
        if server is not None:
            waiters.append(asyncio.ensure_future(server.wait(self._timeout)))
        try:
            done, _ = await asyncio.wait(
                waiters, timeout=self._timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise TimeoutError("no OAuth redirect received")
            winner = next(iter(done))
            if not attempt.callback.done():
                attempt.callback.cancel()  # a late paste is refused as «not pending»
            return winner.result()
        finally:
            for waiter in waiters[1:]:
                waiter.cancel()
            if not attempt.callback.done():
                attempt.callback.cancel()

    @staticmethod
    async def _cancel(attempt: SignInAttempt, code: str) -> None:
        """Stop a pending attempt: mark it, cancel the waiter and free the loopback port."""
        if attempt.status == "pending":
            attempt.status, attempt.error_code = "failed", code
        if attempt.task is not None and not attempt.task.done():
            attempt.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await attempt.task
        if attempt.server is not None:
            await attempt.server.stop()

    async def aclose(self) -> None:
        for attempt in list(self._attempts.values()):
            await self._cancel(attempt, "shutdown")


class _Failed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
