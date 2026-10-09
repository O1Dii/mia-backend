"""``/v1/approvals*`` (contracts/mvp/v1/openapi.yaml, Review §5.3): list, read, decide.

Owner-only: an approval of another user answers 404. Android (bearer) and web
(``mia_session`` cookie, same-origin mutations only) share one principal; the
decision channel is ``android`` for bearer and ``web`` for cookie requests.
``voice_trusted`` is never accepted over HTTP. The client echoes the
``args_hash`` it displayed; any difference is ``409 approval_args_mismatch``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.ports.approval_service import (
    Approval,
    ApprovalError,
    ApprovalStatus,
    DecisionChannel,
    DecisionValue,
)
from app.modules.auth.principal import RequestAuthenticator
from app.modules.tools.errors import ROUTER_CODES

ARGS_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
_HTTP_STATUS = {
    "approval_not_found": 404,
    "approval_args_mismatch": 409,
    "approval_expired": 409,
    "approval_already_decided": 409,
}
_MESSAGES = {
    "approval_expired": "The approval expired.",
}


class ApprovalsBackend(Protocol):
    async def get(self, *, user_id: str, approval_id: str) -> Approval | None: ...

    async def list_for_user(
        self, *, user_id: str, status: ApprovalStatus | None = None, limit: int = 50
    ) -> Sequence[Approval]: ...

    async def decide(
        self,
        *,
        user_id: str,
        approval_id: str,
        decision: DecisionValue,
        args_hash: str,
        channel: DecisionChannel,
        device_id: str | None = None,
    ) -> Approval: ...


class DecisionBody(BaseModel):
    """``ApprovalDecisionRequest``."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]
    args_hash: str = Field(pattern=ARGS_HASH_PATTERN)


def _iso(value: Any) -> str:
    return str(value.isoformat()).replace("+00:00", "Z")


def approval_body(approval: Approval) -> dict[str, Any]:
    """``approval.schema.json#/$defs/Approval``."""
    body: dict[str, Any] = {
        "id": approval.id,
        "run_id": approval.run_id,
        "tool_call_id": approval.tool_call_id,
        "tool": approval.tool,
        "risk": approval.risk,
        "summary": approval.summary,
        "arguments": json.loads(json.dumps(dict(approval.arguments), ensure_ascii=False)),
        "args_hash": approval.args_hash,
        "status": approval.status,
        "created_at": _iso(approval.created_at),
        "expires_at": _iso(approval.expires_at),
        "decision": None,
    }
    if approval.plugin_id is not None:
        body["plugin_id"] = approval.plugin_id
    if approval.decision is not None and approval.status in ("approved", "denied"):
        decision: dict[str, Any] = {
            "decision": approval.decision.decision,
            "decided_at": _iso(approval.decision.decided_at),
            "channel": approval.decision.channel,
        }
        if approval.decision.device_id is not None:
            decision["device_id"] = approval.decision.device_id
        body["decision"] = decision
    return body


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def _json(request: Request, content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=content, headers={"X-Request-ID": _request_id(request)}
    )


def _error(request: Request, code: str) -> JSONResponse:
    known = ROUTER_CODES.get(code)
    message = _MESSAGES.get(code) or (known.message if known else "The request failed.")
    return _json(
        request,
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": _request_id(request),
                "retryable": False,
            }
        },
        _HTTP_STATUS.get(code, 409),
    )


def build_approvals_router(
    *, service: ApprovalsBackend, authenticator: RequestAuthenticator
) -> APIRouter:
    router = APIRouter(prefix="/v1/approvals", tags=["Approvals"])

    def valid_id(approval_id: str) -> bool:
        try:
            uuid.UUID(approval_id)
        except ValueError:
            return False
        return True

    @router.get("")
    async def list_approvals(
        request: Request,
        status: Annotated[
            Literal["pending", "approved", "denied", "expired", "cancelled"] | None, Query()
        ] = "pending",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        principal = await authenticator.authenticate(request)
        items = await service.list_for_user(user_id=principal.user_id, status=status, limit=limit)
        return _json(request, {"items": [approval_body(item) for item in items]})

    @router.get("/{approval_id}")
    async def get_approval(approval_id: str, request: Request) -> JSONResponse:
        principal = await authenticator.authenticate(request)
        approval = (
            await service.get(user_id=principal.user_id, approval_id=approval_id)
            if valid_id(approval_id)
            else None
        )
        if approval is None:
            return _error(request, "approval_not_found")
        return _json(request, approval_body(approval))

    @router.post("/{approval_id}/decision")
    async def decide_approval(
        approval_id: str,
        request: Request,
        body: Annotated[DecisionBody, Body()],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
        ],
    ) -> JSONResponse:
        principal = await authenticator.authenticate(request)
        if not valid_id(approval_id):
            return _error(request, "approval_not_found")
        channel: DecisionChannel = "web" if principal.method == "cookie" else "android"
        try:
            approval = await service.decide(
                user_id=principal.user_id,
                approval_id=approval_id,
                decision=body.decision,
                args_hash=body.args_hash,
                channel=channel,
            )
        except ApprovalError as error:
            if error.code == "approval_already_decided":
                # A retried request (same key or not) with the same verdict replays the result.
                current = await service.get(user_id=principal.user_id, approval_id=approval_id)
                if (
                    current is not None
                    and current.decision is not None
                    and current.decision.decision == body.decision
                    and current.args_hash == body.args_hash
                ):
                    return _json(request, approval_body(current))
            return _error(request, error.code)
        return _json(request, approval_body(approval))

    return router
