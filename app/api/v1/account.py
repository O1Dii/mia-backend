"""``DELETE /v1/account``: «Удалить аккаунт» from the web («Ещё») and Android (settings), M17-R1.

Bearer (Android) or the ``mia_session`` cookie (web, same-origin only); ``Idempotency-Key`` is
required like on every mutation. The steps are in :mod:`app.modules.users.deletion`. The answer
is 204 (the cookie is cleared for a browser). There is nothing to replay: once the account is
gone, the same credentials answer 401 — a client that retries after a lost 204 treats that 401
as done. A failed step answers 503 ``account_deletion_incomplete`` (retryable; the account
still exists and can be deleted again).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Annotated

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.composition import request_authenticator
from app.modules.auth.principal import RequestAuthenticator
from app.modules.runs.domain import TERMINAL_STATUSES, RunNotFoundError
from app.modules.runs.models import RunRow
from app.modules.runs.service import RunService
from app.modules.users.deletion import (
    AccountDeletionIncompleteError,
    AccountDeletionService,
    MemoryShredder,
    ProviderRevoker,
    RunCanceller,
)

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
RUN_STOP_WAIT_S = 5.0


def build_account_router(
    *, service: AccountDeletionService, authenticator: RequestAuthenticator
) -> APIRouter:
    router = APIRouter(prefix="/v1/account", tags=["Account"])

    @router.delete("", status_code=204)
    async def delete_account(request: Request, idempotency_key: IdempotencyKey) -> Response:
        del idempotency_key  # required by the contract; the outcome itself is idempotent
        principal = await authenticator.authenticate(request)
        try:
            await service.delete(uuid.UUID(principal.user_id), requested_via=principal.method)
        except AccountDeletionIncompleteError:
            request_id = str(getattr(request.state, "request_id", "request"))
            return JSONResponse(
                status_code=503,
                headers={"X-Request-ID": request_id},
                content={
                    "error": {
                        "code": "account_deletion_incomplete",
                        "message": "The account was not fully deleted. Try again.",
                        "request_id": request_id,
                        "retryable": True,
                    }
                },
            )
        response = Response(status_code=204)
        if principal.method == "cookie":
            policy = authenticator.policy
            response.delete_cookie(policy.session_cookie, path=policy.path)
        return response

    return router


def run_canceller(
    runs: RunService, session_factory: async_sessionmaker[AsyncSession]
) -> RunCanceller:
    """Cancel the user's unfinished runs and give their tasks a moment to stop."""

    async def cancel(user_id: uuid.UUID) -> int:
        async with session_factory() as session:
            ids = (
                await session.scalars(
                    select(RunRow.id).where(
                        RunRow.user_id == user_id, RunRow.status.not_in(TERMINAL_STATUSES)
                    )
                )
            ).all()
        for run_id in ids:
            with contextlib.suppress(RunNotFoundError):
                await runs.cancel(user_id=user_id, run_id=run_id)
        if ids:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(RUN_STOP_WAIT_S):
                    await asyncio.gather(*(runs.wait_finished(run_id) for run_id in ids))
        return len(ids)

    return cancel


def install_account(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    memory: MemoryShredder | None = None,
    providers: ProviderRevoker | None = None,
    runs: RunService | None = None,
) -> AccountDeletionService:
    """Mount ``DELETE /v1/account`` (after ``install_auth``, memory, connections and runs)."""
    service = AccountDeletionService(
        session_factory,
        memory=memory,
        providers=providers,
        cancel_runs=run_canceller(runs, session_factory) if runs is not None else None,
    )
    app.include_router(
        build_account_router(service=service, authenticator=request_authenticator(app))
    )
    app.state.account = service
    return service
