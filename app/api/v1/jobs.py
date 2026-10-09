"""``/v1/jobs*`` and ``/v1/schedules*`` (contracts/mvp/v1/openapi.yaml, job.schema.json).

Owner-only: another user's job or schedule answers 404. Android (bearer) and web
(``mia_session`` cookie, same-origin mutations) share one principal.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.modules.auth.principal import RequestAuthenticator
from app.modules.conversations.repositories import IdempotencyConflictError
from app.modules.conversations.services import canonical_request_hash
from app.modules.jobs.domain import JobError
from app.modules.jobs.recurrence import RULE_PATTERN, TIMEZONE_PATTERN
from app.modules.jobs.service import JobService

ERRORS: dict[str, tuple[int, str]] = {
    "job_not_found": (404, "The job was not found."),
    "schedule_not_found": (404, "The schedule was not found."),
    "inbox_item_not_found": (404, "The inbox item was not found."),
    "device_not_found": (404, "The device was not found."),
    "invalid_cursor": (400, "The cursor is invalid."),
    "invalid_schedule_rule": (422, "The schedule rule is invalid."),
    "invalid_timezone": (422, "The time zone is unknown."),
    "schedule_interval_too_short": (422, "The schedule runs too often."),
    "job_limit_exceeded": (409, "Too many active background jobs."),
    "schedule_limit_exceeded": (409, "Too many schedules."),
    "notify_rate_limited": (429, "Too many notifications in the last hour."),
    "idempotency_conflict": (409, "The idempotency key was reused for a different request."),
}
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
Channel = Literal["push", "telegram", "inbox"]


def request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def json_response(request: Request, content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=content, headers={"X-Request-ID": request_id(request)}
    )


def empty_response(request: Request, status_code: int = 204) -> Response:
    return Response(status_code=status_code, headers={"X-Request-ID": request_id(request)})


def error_response(request: Request, code: str) -> JSONResponse:
    status, message = ERRORS.get(code, (409, "The request failed."))
    return json_response(
        request,
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id(request),
                "retryable": code == "notify_rate_limited",
            }
        },
        status,
    )


class DeliveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: list[Channel] = Field(min_length=1)


class BudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_steps: int = Field(ge=1, le=200)
    max_duration_seconds: int = Field(ge=1, le=1800)
    max_cost_usd: float | None = Field(default=None, ge=0)


class CreateJobBody(BaseModel):
    """``CreateJobRequest``."""

    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=4000)
    inputs: dict[str, Any] | None = None
    delivery: DeliveryBody | None = None
    budget: BudgetBody | None = None


class CreateScheduleBody(BaseModel):
    """``CreateScheduleRequest``."""

    model_config = ConfigDict(extra="forbid")
    rule: str = Field(max_length=1000, pattern=RULE_PATTERN.pattern)
    timezone: str = Field(max_length=64, pattern=TIMEZONE_PATTERN.pattern)
    goal: str = Field(min_length=1, max_length=4000)
    delivery: DeliveryBody
    misfire_policy: Literal["skip", "run_once", "limited_catch_up"] = "skip"
    catch_up_limit: int = Field(default=3, ge=1, le=24)
    kind: Literal["run", "reminder"] = "run"


class UpdateScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "paused"] | None = None
    rule: str | None = Field(default=None, max_length=1000, pattern=RULE_PATTERN.pattern)
    timezone: str | None = Field(default=None, max_length=64, pattern=TIMEZONE_PATTERN.pattern)
    goal: str | None = Field(default=None, min_length=1, max_length=4000)


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def build_jobs_router(*, service: JobService, authenticator: RequestAuthenticator) -> APIRouter:
    router = APIRouter(tags=["Background"])

    async def user_of(request: Request) -> uuid.UUID:
        principal = await authenticator.authenticate(request)
        return uuid.UUID(principal.user_id)

    @router.get("/v1/jobs")
    async def list_jobs(
        request: Request,
        cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        user = await user_of(request)
        try:
            jobs, following = await service.list_jobs(user_id=user, limit=limit, cursor=cursor)
        except JobError as error:
            return error_response(request, error.code)
        return json_response(
            request, {"items": [job.to_wire() for job in jobs], "next_cursor": following}
        )

    @router.post("/v1/jobs", status_code=202)
    async def create_job(
        request: Request, body: Annotated[CreateJobBody, Body()], idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        user = await user_of(request)
        payload = body.model_dump(mode="json", exclude_none=True)
        try:
            job, _ = await service.create_job(
                user_id=user,
                request=payload,
                origin_type="user",
                origin_id=str(user),
                idempotency_key=idempotency_key,
                request_hash=canonical_request_hash(payload),
            )
        except IdempotencyConflictError:
            return error_response(request, "idempotency_conflict")
        except JobError as error:
            return error_response(request, error.code)
        return json_response(request, job.to_wire(), 202)

    @router.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> JSONResponse:
        user = await user_of(request)
        parsed = _uuid(job_id)
        try:
            if parsed is None:
                raise JobError("job_not_found")
            job = await service.get_job(user_id=user, job_id=parsed)
        except JobError as error:
            return error_response(request, error.code)
        return json_response(request, job.to_wire())

    @router.post("/v1/jobs/{job_id}/cancel", status_code=202)
    async def cancel_job(
        job_id: str, request: Request, idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        user = await user_of(request)
        parsed = _uuid(job_id)
        try:
            if parsed is None:
                raise JobError("job_not_found")
            job = await service.cancel_job(user_id=user, job_id=parsed)
        except JobError as error:
            return error_response(request, error.code)
        return json_response(request, job.to_wire(), 202)

    @router.get("/v1/schedules")
    async def list_schedules(request: Request) -> JSONResponse:
        user = await user_of(request)
        schedules = await service.list_schedules(user_id=user)
        return json_response(request, {"items": [s.to_wire() for s in schedules]})

    @router.post("/v1/schedules", status_code=201)
    async def create_schedule(
        request: Request,
        body: Annotated[CreateScheduleBody, Body()],
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        user = await user_of(request)
        payload = body.model_dump(mode="json")
        try:
            schedule, _ = await service.create_schedule(
                user_id=user,
                request=payload,
                idempotency_key=idempotency_key,
                request_hash=canonical_request_hash(payload),
            )
        except IdempotencyConflictError:
            return error_response(request, "idempotency_conflict")
        except JobError as error:
            return error_response(request, error.code)
        return json_response(request, schedule.to_wire(), 201)

    @router.patch("/v1/schedules/{schedule_id}")
    async def update_schedule(
        schedule_id: str,
        request: Request,
        body: Annotated[UpdateScheduleBody, Body()],
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        user = await user_of(request)
        changes = body.model_dump(exclude_none=True)
        parsed = _uuid(schedule_id)
        try:
            if parsed is None:
                raise JobError("schedule_not_found")
            if not changes:
                schedule = await service.get_schedule(user_id=user, schedule_id=parsed)
            else:
                schedule = await service.update_schedule(
                    user_id=user, schedule_id=parsed, changes=changes
                )
        except JobError as error:
            return error_response(request, error.code)
        return json_response(request, schedule.to_wire())

    @router.delete("/v1/schedules/{schedule_id}", status_code=204)
    async def delete_schedule(
        schedule_id: str, request: Request, idempotency_key: IdempotencyKey
    ) -> Response:
        user = await user_of(request)
        parsed = _uuid(schedule_id)
        try:
            if parsed is None:
                raise JobError("schedule_not_found")
            await service.delete_schedule(user_id=user, schedule_id=parsed)
        except JobError as error:
            return error_response(request, error.code)
        return empty_response(request)

    @router.post("/v1/schedules/{schedule_id}/run", status_code=202)
    async def run_schedule_now(
        schedule_id: str, request: Request, idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        user = await user_of(request)
        parsed = _uuid(schedule_id)
        try:
            if parsed is None:
                raise JobError("schedule_not_found")
            job = await service.run_schedule_now(
                user_id=user, schedule_id=parsed, idempotency_key=idempotency_key
            )
        except IdempotencyConflictError:
            return error_response(request, "idempotency_conflict")
        except JobError as error:
            return error_response(request, error.code)
        return json_response(request, job.to_wire(), 202)

    return router
