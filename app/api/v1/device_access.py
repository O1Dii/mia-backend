"""Device access API for the phone (ADR-0016, M70; ``device-access.schema.json``).

Only the app on the device itself (bearer of a session signed in on that device) may use
``/v1/devices/{device_id}/…`` here; any other caller gets ``403 device_access_forbidden``.

- ``GET  /v1/devices/{device_id}/tool-calls`` — calls queued for this device (push delivery);
- ``GET  /v1/devices/{device_id}/grants`` — persistent permissions given to other clients;
- ``DELETE /v1/devices/{device_id}/grants/{grant_id}`` — revoke one;
- ``GET  /v1/devices/{device_id}/access-log`` — what other clients asked, when, the decision;
- ``GET  /v1/devices/{device_id}/capabilities`` and
  ``PUT  /v1/devices/{device_id}/capabilities/{capability}`` — may other clients ask
  (``ask``) or never (``deny``).

M84 («Устройства»): listing and revoking grants and reading the access log are also open to
any other signed-in session of the phone's owner (``account_access``) — they show names and
decisions only and revoking only takes access away. Queued calls and the capability switches
stay with the phone itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from app.core.ports.device_tool_channel import DeviceToolCall
from app.modules.auth.principal import RequestAuthenticator, RequestPrincipal
from app.modules.tools.device import call_body
from app.modules.tools.device_access import DeviceAccess
from app.modules.tools.device_policy import CAPABILITIES, capability_for
from app.modules.tools.device_store import AccessLogEntry, GrantRecord
from app.modules.tools.errors import DEVICE_ACCESS_CODES

AccountAccess = Callable[[str, str], Awaitable[bool]]
"""``(user_id, device_id)`` → is it a phone of this account (M84)."""


class PendingCalls(Protocol):
    def pending_for_device(self, *, user_id: str, device_id: str) -> list[DeviceToolCall]: ...


class CapabilityBody(BaseModel):
    """``DeviceCapabilitySettingRequest``."""

    model_config = ConfigDict(extra="forbid")
    remote: Literal["ask", "deny"]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _requester(kind: str, label: str) -> dict[str, str]:
    return {"kind": kind, "label": label}


def grant_body(grant: GrantRecord) -> dict[str, Any]:
    """``DeviceGrant``."""
    capability = capability_for(grant.capability)
    return {
        "id": grant.id,
        "capability": grant.capability,
        "capability_label": capability.label,
        "requester": _requester(grant.requester_kind, grant.requester_label),
        "created_at": _iso(grant.created_at),
        "expires_at": _iso(grant.expires_at),
        "last_used_at": _iso(grant.last_used_at),
    }


def log_body(entry: AccessLogEntry, now: datetime) -> dict[str, Any]:
    """``DeviceAccessLogEntry``; a lapsed pending request reads ``expired``."""
    decision = entry.decision
    if decision == "pending" and entry.expires_at is not None and entry.expires_at <= now:
        decision = "expired"
    body: dict[str, Any] = {
        "id": entry.id,
        "capability": entry.capability,
        "capability_label": capability_for(entry.capability).label,
        "tool": entry.tool,
        "requester": _requester(entry.requester_kind, entry.requester_label),
        "decision": decision,
        "created_at": _iso(entry.created_at),
    }
    if entry.run_id is not None:
        body["run_id"] = entry.run_id
    return body


def setting_body(name: str, settings: dict[str, str]) -> dict[str, Any]:
    """``DeviceCapabilitySetting``."""
    capability = CAPABILITIES[name]
    return {
        "capability": name,
        "kind": capability.kind,
        "label": capability.label,
        "model_sees": capability.model_sees,
        "persistent_allowed": capability.persistent_allowed,
        "remote": "deny" if settings.get(name) == "deny" else "ask",
    }


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def _json(request: Request, content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=content, headers={"X-Request-ID": _request_id(request)}
    )


def device_error(request: Request, code: str) -> JSONResponse:
    known = DEVICE_ACCESS_CODES[code]
    return _json(
        request,
        {
            "error": {
                "code": code,
                "message": known.message,
                "request_id": _request_id(request),
                "retryable": known.retryable,
            }
        },
        known.http_status or 409,
    )


def build_device_access_router(
    *,
    access: DeviceAccess,
    calls: PendingCalls,
    authenticator: RequestAuthenticator,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    account_access: AccountAccess | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/devices/{device_id}", tags=["Devices"])

    async def own_device(
        request: Request, device_id: uuid.UUID
    ) -> tuple[RequestPrincipal, JSONResponse | None]:
        principal = await authenticator.authenticate(request)
        if principal.method != "bearer":
            return principal, device_error(request, "device_access_forbidden")
        device = await access.store.session_device(
            user_id=principal.user_id, family_id=principal.session_id
        )
        if device != str(device_id):
            return principal, device_error(request, "device_access_forbidden")
        access.seen(user_id=principal.user_id, device_id=device)
        return principal, None

    async def account_device(
        request: Request, device_id: uuid.UUID
    ) -> tuple[RequestPrincipal, JSONResponse | None]:
        """The phone itself, or (M84) another session of its owner."""
        principal = await authenticator.authenticate(request)
        if principal.method == "bearer":
            device = await access.store.session_device(
                user_id=principal.user_id, family_id=principal.session_id
            )
            if device == str(device_id):
                access.seen(user_id=principal.user_id, device_id=device)
                return principal, None
        if account_access is not None and await account_access(principal.user_id, str(device_id)):
            return principal, None
        return principal, device_error(request, "device_access_forbidden")

    @router.get("/tool-calls")
    async def list_tool_calls(device_id: uuid.UUID, request: Request) -> JSONResponse:
        principal, denied = await own_device(request, device_id)
        if denied is not None:
            return denied
        pending: Sequence[DeviceToolCall] = calls.pending_for_device(
            user_id=principal.user_id, device_id=str(device_id)
        )
        return _json(request, {"items": [call_body(call) for call in pending]})

    @router.get("/grants")
    async def list_grants(device_id: uuid.UUID, request: Request) -> JSONResponse:
        principal, denied = await account_device(request, device_id)
        if denied is not None:
            return denied
        grants = await access.store.list_grants(
            user_id=principal.user_id, device_id=str(device_id), now=clock()
        )
        return _json(request, {"items": [grant_body(grant) for grant in grants]})

    @router.delete("/grants/{grant_id}", status_code=204)
    async def revoke_grant(
        device_id: uuid.UUID,
        grant_id: uuid.UUID,
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=16, max_length=128)
        ] = None,
    ) -> Response:
        del idempotency_key  # revoking twice answers 404 the second time
        principal, denied = await account_device(request, device_id)
        if denied is not None:
            return denied
        revoked = await access.store.revoke_grant(
            user_id=principal.user_id, device_id=str(device_id), grant_id=str(grant_id), now=clock()
        )
        if not revoked:
            return device_error(request, "device_grant_not_found")
        return Response(status_code=204, headers={"X-Request-ID": _request_id(request)})

    @router.get("/access-log")
    async def access_log(
        device_id: uuid.UUID,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        principal, denied = await account_device(request, device_id)
        if denied is not None:
            return denied
        entries = await access.store.access_log(
            user_id=principal.user_id, device_id=str(device_id), limit=limit
        )
        now = clock()
        return _json(request, {"items": [log_body(entry, now) for entry in entries]})

    @router.get("/capabilities")
    async def list_capabilities(device_id: uuid.UUID, request: Request) -> JSONResponse:
        principal, denied = await own_device(request, device_id)
        if denied is not None:
            return denied
        settings = await access.store.remote_settings(
            user_id=principal.user_id, device_id=str(device_id)
        )
        return _json(request, {"items": [setting_body(name, settings) for name in CAPABILITIES]})

    @router.put("/capabilities/{capability}")
    async def put_capability(
        device_id: uuid.UUID,
        capability: str,
        request: Request,
        body: Annotated[CapabilityBody, Body()],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=16, max_length=128)
        ] = None,
    ) -> JSONResponse:
        del idempotency_key  # setting a value is naturally idempotent
        principal, denied = await own_device(request, device_id)
        if denied is not None:
            return denied
        if capability not in CAPABILITIES:
            return device_error(request, "device_capability_unknown")
        await access.store.set_remote(
            user_id=principal.user_id,
            device_id=str(device_id),
            capability=capability,
            remote=body.remote,
            now=clock(),
        )
        settings = await access.store.remote_settings(
            user_id=principal.user_id, device_id=str(device_id)
        )
        return _json(request, setting_body(capability, settings))

    return router
