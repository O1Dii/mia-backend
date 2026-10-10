"""``GET /v1/now`` (contracts/mvp/v1/openapi.yaml, M74): the «Сейчас» strip.

Active timers, alarms and focus sessions of the builtin plugins plus active background
jobs of the account (shared by all chats). Clients count down locally from
``remaining_ms`` against ``server_time`` and refresh on focus, after a run that touched
a timer/alarm/job, or every ~30 s while visible. ``cancel`` on each item says how to stop
it without the model.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.modules.auth.principal import RequestAuthenticator
from app.modules.jobs.now import NowService


def build_now_router(*, service: NowService, authenticator: RequestAuthenticator) -> APIRouter:
    router = APIRouter(tags=["Now"])

    @router.get("/v1/now")
    async def get_now(request: Request) -> JSONResponse:
        principal = await authenticator.authenticate(request)
        body = await service.now(user_id=uuid.UUID(principal.user_id))
        request_id = str(
            getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request"))
        )
        return JSONResponse(
            content=body, headers={"X-Request-ID": request_id, "Cache-Control": "no-store"}
        )

    return router
