"""«Устройства» API (M84; ``devices.schema.json``): the account's devices in one list.

Any signed-in session of the account (the app's bearer, the WebView, a browser) may use these:

- ``GET   /v1/devices`` — phones, browsers (Personal Nodes from M86) with presence, plus the
  «Облако Mia» row (what works without devices);
- ``GET   /v1/devices/{device_id}`` — the card: capabilities (on in Mia, OS permission, which
  installed plugins use them), persistent grants, the access log link;
- ``PATCH /v1/devices/{device_id}`` — rename (``{"name": null}`` returns the default name);
- ``POST  /v1/devices/{device_id}/disconnect`` — sign that device out, drop its push token and
  permissions (``409 device_is_current`` for the device making the request);
- ``POST  /v1/devices/heartbeat`` — «пульс» of an open app or tab; the app on a phone adds its
  status report (OS permission per capability).

The phone-only paths of ADR-0016 (``/v1/devices/{id}/tool-calls``, ``…/capabilities``) stay in
``app/api/v1/device_access.py``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.modules.auth.principal import RequestAuthenticator
from app.modules.devices.report import DeviceStatusReport
from app.modules.devices.service import NAME_MAX, DeviceDirectory

ERRORS: dict[str, tuple[int, str]] = {
    "device_not_found": (404, "The device was not found."),
    "device_is_current": (409, "This is the device you are using; sign out instead."),
}
OptionalIdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=16, max_length=128)
]


class HeartbeatBody(BaseModel):
    """``DeviceHeartbeatRequest``."""

    model_config = ConfigDict(extra="forbid")
    report: DeviceStatusReport | None = None


class RenameBody(BaseModel):
    """``DeviceRenameRequest``."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(max_length=NAME_MAX)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def _json(request: Request, content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=content, headers={"X-Request-ID": _request_id(request)}
    )


def _error(request: Request, code: str) -> JSONResponse:
    status, message = ERRORS[code]
    body = {
        "error": {
            "code": code,
            "message": message,
            "request_id": _request_id(request),
            "retryable": False,
        }
    }
    return _json(request, body, status)


def build_device_directory_router(
    *, directory: DeviceDirectory, authenticator: RequestAuthenticator
) -> APIRouter:
    router = APIRouter(prefix="/v1/devices", tags=["Devices"])

    @router.get("")
    async def list_devices(request: Request) -> JSONResponse:
        principal = await authenticator.authenticate(request)
        return _json(request, await directory.list(principal))

    @router.post("/heartbeat")
    async def heartbeat(
        request: Request,
        idempotency_key: OptionalIdempotencyKey = None,
        body: Annotated[HeartbeatBody | None, Body()] = None,
    ) -> JSONResponse:
        del idempotency_key  # a heartbeat only refreshes «онлайн»; repeating it is harmless
        principal = await authenticator.authenticate(request)
        identity = await directory.heartbeat(
            principal,
            body.report if body is not None else None,
            user_agent=request.headers.get("user-agent"),
        )
        if identity is None:
            return _error(request, "device_not_found")
        return _json(request, directory.heartbeat_body(identity))

    @router.get("/{device_id}")
    async def get_device(device_id: uuid.UUID, request: Request) -> JSONResponse:
        principal = await authenticator.authenticate(request)
        body = await directory.detail(principal, str(device_id))
        if body is None:
            return _error(request, "device_not_found")
        return _json(request, body)

    @router.patch("/{device_id}")
    async def rename_device(
        device_id: uuid.UUID,
        request: Request,
        body: Annotated[RenameBody, Body()],
        idempotency_key: OptionalIdempotencyKey = None,
    ) -> JSONResponse:
        del idempotency_key  # setting a name is naturally idempotent
        principal = await authenticator.authenticate(request)
        renamed = await directory.rename(principal, str(device_id), body.name)
        if renamed is None:
            return _error(request, "device_not_found")
        return _json(request, renamed)

    @router.post("/{device_id}/disconnect", status_code=204)
    async def disconnect_device(
        device_id: uuid.UUID,
        request: Request,
        idempotency_key: OptionalIdempotencyKey = None,
    ) -> Response:
        del idempotency_key  # a second call answers 404: the device is no longer connected
        principal = await authenticator.authenticate(request)
        result = await directory.disconnect(principal, str(device_id))
        if result == "not_found":
            return _error(request, "device_not_found")
        if result == "current":
            return _error(request, "device_is_current")
        return Response(status_code=204, headers={"X-Request-ID": _request_id(request)})

    return router
