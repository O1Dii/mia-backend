"""Device tools API (``contracts/mvp/v1/device-tools.schema.json``, Review §5.4.2, §7.5).

``PUT /v1/devices/{device_id}/tools`` replaces the tool declarations of one
first-party device (Android); ``POST /v1/runs/{run_id}/device-tool-results``
carries the result of a ``device_tool.call`` run event back to the waiting
:class:`~app.core.ports.device_tool_channel.DeviceToolChannel`.

Rules: bearer principals only (the phone), owner-only runs (another user's run
answers 404), the result is accepted only for a call that is pending for this
run on the run's device. Tool declarations are kept in process memory (the
channel) and contain no personal data; the results the model sees are the
compact ``content`` of the call, and nothing the device returns (for example
contact search hits) is persisted by this module.
"""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.ports.device_tool_channel import (
    DeviceToolDeclaration,
    DeviceToolErrorCode,
    DeviceToolResult,
)
from app.core.ports.types import Risk
from app.modules.auth.principal import RequestAuthenticator, RequestPrincipal
from app.modules.runs.domain import RunNotFoundError, RunRecord

logger = logging.getLogger(__name__)

TOOL_NAME_PATTERN = r"^(device|telegram)\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"
MAX_DECLARED_TOOLS = 64
DELIVERED_MEMORY = 2048
"""Delivered ``(user, run, call)`` keys remembered to answer a retried POST with 204."""


class DeclarationBody(BaseModel):
    """``DeviceToolDeclaration``."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=5, max_length=1000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    risk: Risk
    voice: bool
    requires_unlocked: bool
    timeout_ms: int = Field(ge=100, le=60000)

    @model_validator(mode="after")
    def _object_schema(self) -> DeclarationBody:
        if self.input_schema.get("type") != "object":
            raise ValueError("input_schema must describe an object")
        return self


class RegistrationBody(BaseModel):
    """``DeviceToolRegistration``."""

    model_config = ConfigDict(extra="forbid")
    device_id: uuid.UUID
    platform: Literal["android"]
    app_version: str = Field(min_length=1, max_length=64)
    tools: list[DeclarationBody] = Field(max_length=MAX_DECLARED_TOOLS)


class ErrorBody(BaseModel):
    """``DeviceToolError``."""

    model_config = ConfigDict(extra="forbid")
    code: DeviceToolErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool


class ResultBody(BaseModel):
    """``DeviceToolResult``: ``succeeded`` needs content, the others an ``error``."""

    model_config = ConfigDict(extra="forbid")
    call_id: str = Field(min_length=1, max_length=128)
    status: Literal["succeeded", "failed", "uncertain"]
    content: str | None = Field(default=None, max_length=8000)
    structured_content: dict[str, Any] | None = None
    error: ErrorBody | None = None

    @model_validator(mode="after")
    def _shape(self) -> ResultBody:
        if self.status == "succeeded":
            if self.content is None or self.error is not None:
                raise ValueError("a succeeded result needs content and no error")
        elif self.error is None:
            raise ValueError("a failed or uncertain result needs an error")
        return self

    def to_port(self) -> DeviceToolResult:
        return DeviceToolResult(
            call_id=self.call_id,
            status=self.status,
            content=self.content or "",
            structured_content=self.structured_content,
            error_code=self.error.code if self.error else None,
            error_message=self.error.message if self.error else None,
            retryable=self.error.retryable if self.error else False,
        )


class DeviceToolsBackend(Protocol):
    """The slice of ``InMemoryDeviceToolChannel`` this API uses."""

    async def register(
        self, *, user_id: str, device_id: str, tools: Sequence[DeviceToolDeclaration]
    ) -> None: ...

    async def deliver_result(
        self, *, user_id: str, device_id: str, result: DeviceToolResult
    ) -> bool: ...

    def pending_run(self, *, user_id: str, device_id: str, call_id: str) -> str | None: ...


class RunsLookup(Protocol):
    """``RunService.get``: owner-scoped, raises ``RunNotFoundError``."""

    async def get(self, *, user_id: uuid.UUID, run_id: uuid.UUID) -> RunRecord: ...


class DeviceAuthenticator(Protocol):
    async def authenticate(self, request: Request) -> RequestPrincipal: ...


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def _error(
    request: Request, status_code: int, code: str, message: str, *, retryable: bool = False
) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_code,
        headers={"X-Request-ID": request_id},
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "retryable": retryable,
            }
        },
    )


class _ValidatedRoute(APIRoute):
    """Validation failures use the public envelope without echoing the input."""

    def get_route_handler(self) -> Any:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response: Response = await original(request)
            except RequestValidationError:
                return _error(request, 422, "validation_error", "The request is invalid.")
            return response

        return handle


def _declaration(body: DeclarationBody) -> DeviceToolDeclaration:
    return DeviceToolDeclaration(
        name=body.name,
        description=body.description,
        input_schema=body.input_schema,
        risk=body.risk,
        voice=body.voice,
        requires_unlocked=body.requires_unlocked,
        timeout_ms=body.timeout_ms,
    )


def build_device_tools_router(
    *,
    channel: DeviceToolsBackend,
    runs: RunsLookup,
    authenticator: DeviceAuthenticator,
) -> APIRouter:
    router = APIRouter(tags=["Devices"], route_class=_ValidatedRoute)
    delivered: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    def remember(key: tuple[str, str, str]) -> None:
        delivered[key] = None
        while len(delivered) > DELIVERED_MEMORY:
            delivered.popitem(last=False)

    def not_a_device_principal(
        request: Request, principal: RequestPrincipal
    ) -> JSONResponse | None:
        if principal.method != "bearer":
            return _error(request, 403, "permission_denied", "Only the app can use this endpoint.")
        return None

    @router.put("/v1/devices/{device_id}/tools", status_code=204)
    async def put_device_tools(
        device_id: uuid.UUID,
        body: RegistrationBody,
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=16, max_length=128)
        ] = None,
    ) -> Response:
        del idempotency_key  # replacing the declaration set is naturally idempotent
        principal = await authenticator.authenticate(request)
        if (denied := not_a_device_principal(request, principal)) is not None:
            return denied
        if body.device_id != device_id:
            return _error(request, 422, "validation_error", "The device id does not match.")
        names = [tool.name for tool in body.tools]
        if len(set(names)) != len(names):
            return _error(request, 422, "validation_error", "Tool names must be unique.")
        await channel.register(
            user_id=principal.user_id,
            device_id=str(device_id),
            tools=[_declaration(tool) for tool in body.tools],
        )
        logger.info("device_tools_registered", extra={"tools": len(names)})
        return Response(status_code=204, headers={"X-Request-ID": _request_id(request)})

    @router.post("/v1/runs/{run_id}/device-tool-results", status_code=204)
    async def post_device_tool_result(
        run_id: uuid.UUID,
        body: ResultBody,
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=16, max_length=128)
        ] = None,
    ) -> Response:
        del idempotency_key  # a retried delivery is recognised by (run, call_id)
        principal = await authenticator.authenticate(request)
        if (denied := not_a_device_principal(request, principal)) is not None:
            return denied
        try:
            record = await runs.get(user_id=uuid.UUID(principal.user_id), run_id=run_id)
        except RunNotFoundError:
            return _error(request, 404, "run_not_found", "The run was not found.")
        key = (principal.user_id, str(run_id), body.call_id)
        no_call = _error(
            request,
            409,
            "device_call_not_pending",
            "No device tool call is waiting for this result.",
        )
        if record.device_id is None:
            return no_call
        device_id = str(record.device_id)
        pending = channel.pending_run(
            user_id=principal.user_id, device_id=device_id, call_id=body.call_id
        )
        if pending != str(run_id):
            return Response(status_code=204) if key in delivered else no_call
        accepted = await channel.deliver_result(
            user_id=principal.user_id, device_id=device_id, result=body.to_port()
        )
        if not accepted:
            return Response(status_code=204) if key in delivered else no_call
        remember(key)
        return Response(status_code=204, headers={"X-Request-ID": _request_id(request)})

    return router


def install_device_tools(
    app: FastAPI,
    *,
    channel: DeviceToolsBackend,
    runs: RunsLookup,
    authenticator: RequestAuthenticator,
) -> None:
    """Mount the two endpoints (``app/composition.py``)."""
    app.include_router(
        build_device_tools_router(channel=channel, runs=runs, authenticator=authenticator)
    )
