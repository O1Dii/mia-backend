"""``PUT /v1/devices/{id}/tools`` and ``POST /v1/runs/{id}/device-tool-results`` (M23)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.api.v1.device_tools import build_device_tools_router
from app.core.correlation import CorrelationMiddleware
from app.core.ports.device_tool_channel import DeviceToolCall
from app.modules.auth.principal import AuthenticationFailure, RequestPrincipal
from app.modules.auth.router import auth_error_handler
from app.modules.runs.domain import RunNotFoundError, RunRecord
from app.modules.tools.device import InMemoryDeviceToolChannel
from fastapi import FastAPI, Request

from tests.fakes import assert_mvp_valid

OWNER = uuid.uuid4()
STRANGER = uuid.uuid4()
DEVICE = uuid.uuid4()
KEY = "idem-0123456789abcdef"
ERROR_CODES = {
    entry["code"]: entry
    for entry in json.loads(Path("contracts/mvp/v1/error-codes.json").read_text("utf-8"))
}


class Authenticator:
    """Bearer tokens ``owner`` / ``stranger``; ``cookie`` is a web principal."""

    async def authenticate(self, request: Request) -> RequestPrincipal:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ")
        users = {"owner": OWNER, "stranger": STRANGER}
        if token == "cookie":
            return RequestPrincipal(
                user_id=str(OWNER),
                method="cookie",
                session_id="s",
                user=None,  # type: ignore[arg-type]
            )
        if token not in users:
            raise AuthenticationFailure(
                401, "authentication_required", "Authentication is required."
            )
        return RequestPrincipal(
            user_id=str(users[token]),
            method="bearer",
            session_id="s",
            user=None,  # type: ignore[arg-type]
        )


class Runs:
    def __init__(self) -> None:
        self.records: dict[uuid.UUID, RunRecord] = {}

    def add(self, *, user: uuid.UUID = OWNER, device: uuid.UUID | None = DEVICE) -> uuid.UUID:
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        self.records[run_id] = RunRecord(
            id=run_id,
            user_id=user,
            profile="voice",
            status="running",
            created_at=now,
            updated_at=now,
            device_id=device,
        )
        return run_id

    async def get(self, *, user_id: uuid.UUID, run_id: uuid.UUID) -> RunRecord:
        record = self.records.get(run_id)
        if record is None or record.user_id != user_id:
            raise RunNotFoundError("run was not found")
        return record


class Publisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.published = asyncio.Event()

    async def __call__(
        self, *, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        self.events.append({"run_id": run_id, "type": event_type, **payload})
        self.published.set()


def declaration(name: str = "device.contacts.search", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "description": "Search the phone contacts by name.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 80}},
            "required": ["query"],
        },
        "risk": "read",
        "voice": True,
        "requires_unlocked": False,
        "timeout_ms": 5000,
    }
    body.update(overrides)
    return body


def registration(*tools: dict[str, Any], device: uuid.UUID = DEVICE) -> dict[str, Any]:
    return {
        "device_id": str(device),
        "platform": "android",
        "app_version": "0.1.0",
        "tools": list(tools) or [declaration()],
    }


class Harness:
    def __init__(self, client: httpx.AsyncClient, channel: InMemoryDeviceToolChannel, runs: Runs):
        self.client = client
        self.channel = channel
        self.runs = runs
        self.publisher = Publisher()
        channel.bind_publisher(self.publisher)

    async def put(
        self, body: dict[str, Any], *, device: uuid.UUID = DEVICE, token: str = "owner"
    ) -> httpx.Response:
        return await self.client.put(
            f"/v1/devices/{device}/tools",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": KEY},
        )

    async def post(
        self, run_id: uuid.UUID | str, body: dict[str, Any], *, token: str = "owner"
    ) -> httpx.Response:
        return await self.client.post(
            f"/v1/runs/{run_id}/device-tool-results",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": KEY},
        )

    def call(self, run_id: uuid.UUID, call_id: str, tool: str = "device.contacts.search") -> Any:
        """Start a device call as the tool router would; returns the invoke task."""
        return asyncio.create_task(
            self.channel.invoke(
                user_id=str(OWNER),
                call=DeviceToolCall(
                    call_id=call_id,
                    run_id=str(run_id),
                    device_id=str(DEVICE),
                    tool=tool,
                    arguments={"query": "Анна"},
                    deadline_at=datetime.now(UTC) + timedelta(seconds=5),
                ),
            )
        )


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)
    app.add_exception_handler(AuthenticationFailure, auth_error_handler)
    runs = Runs()
    channel = InMemoryDeviceToolChannel()
    app.include_router(
        build_device_tools_router(channel=channel, runs=runs, authenticator=Authenticator())
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Harness(client, channel, runs)


async def registered(harness: Harness) -> None:
    response = await harness.put(registration(declaration()))
    assert response.status_code == 204


async def test_registration_is_contract_valid_and_replaces_the_set(harness: Harness) -> None:
    body = registration(
        declaration(),
        declaration("device.alarm.set", risk="write_reversible", requires_unlocked=True),
        declaration("telegram.handoff", risk="write_external", voice=False),
    )
    assert_mvp_valid(body, "device-tools")
    assert (await harness.put(body)).status_code == 204
    tools = await harness.channel.available_tools(user_id=str(OWNER), device_id=str(DEVICE))
    assert {tool.name for tool in tools} == {
        "device.contacts.search",
        "device.alarm.set",
        "telegram.handoff",
    }
    alarm = next(tool for tool in tools if tool.name == "device.alarm.set")
    assert (alarm.risk, alarm.requires_unlocked, alarm.timeout_ms) == (
        "write_reversible",
        True,
        5000,
    )
    assert (await harness.put(registration(declaration("device.app.open")))).status_code == 204
    tools = await harness.channel.available_tools(user_id=str(OWNER), device_id=str(DEVICE))
    assert [tool.name for tool in tools] == ["device.app.open"]


async def test_registration_is_scoped_to_the_user(harness: Harness) -> None:
    await registered(harness)
    assert (
        await harness.put(registration(declaration("device.app.open")), token="stranger")
    ).status_code == 204
    mine = await harness.channel.available_tools(user_id=str(OWNER), device_id=str(DEVICE))
    assert [tool.name for tool in mine] == ["device.contacts.search"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(platform="ios"),
        lambda body: body.update(tools=[declaration("calendar.read")]),
        lambda body: body.update(tools=[declaration(timeout_ms=10)]),
        lambda body: body.update(tools=[declaration(risk="delete")]),
        lambda body: body.update(tools=[declaration(input_schema={"type": "string"})]),
        lambda body: body.update(tools=[declaration(), declaration()]),
        lambda body: body.update(tools=[declaration(f"device.t{i}") for i in range(65)]),
        lambda body: body.update(extra="x"),
        lambda body: body.update(device_id=str(uuid.uuid4())),
    ],
)
async def test_registration_rejects_invalid_bodies(harness: Harness, mutate: Any) -> None:
    body = registration()
    mutate(body)
    response = await harness.put(body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert await harness.channel.available_tools(user_id=str(OWNER), device_id=str(DEVICE)) == []


async def test_authentication_is_required_and_cookie_is_refused(harness: Harness) -> None:
    assert (await harness.put(registration(), token="nobody")).status_code == 401
    cookie = await harness.put(registration(), token="cookie")
    assert cookie.status_code == 403
    run_id = harness.runs.add()
    assert (
        await harness.post(
            run_id, {"call_id": "c", "status": "succeeded", "content": "x"}, token="cookie"
        )
    ).status_code == 403


async def test_result_round_trip_unblocks_the_waiting_call(harness: Harness) -> None:
    await registered(harness)
    run_id = harness.runs.add()
    task = harness.call(run_id, "call-1")
    await asyncio.wait_for(harness.publisher.published.wait(), 2)
    assert harness.publisher.events[0]["type"] == "device_tool.call"
    body = {
        "call_id": "call-1",
        "status": "succeeded",
        "content": "1 contact: Анна",
        "structured_content": {"matches": [{"display_name": "Анна"}]},
    }
    assert_mvp_valid(body, "device-tools", "DeviceToolResult")
    response = await harness.post(run_id, body)
    assert response.status_code == 204 and response.content == b""
    result = await asyncio.wait_for(task, 2)
    assert result.status == "succeeded" and result.content == "1 contact: Анна"
    # M70: contact results keep names and counts only, in one shape.
    assert result.structured_content == {"matches": [{"name": "Анна"}]}


async def test_structured_errors_reach_the_channel(harness: Harness) -> None:
    await registered(harness)
    run_id = harness.runs.add()
    task = harness.call(run_id, "call-2")
    await asyncio.wait_for(harness.publisher.published.wait(), 2)
    body = {
        "call_id": "call-2",
        "status": "failed",
        "error": {
            "code": "os_permission_denied",
            "message": "READ_CONTACTS was denied.",
            "retryable": False,
        },
    }
    assert_mvp_valid(body, "device-tools", "DeviceToolResult")
    assert (await harness.post(run_id, body)).status_code == 204
    result = await asyncio.wait_for(task, 2)
    assert (result.status, result.error_code, result.retryable) == (
        "failed",
        "os_permission_denied",
        False,
    )


async def test_retried_delivery_is_answered_204_but_a_late_one_is_409(harness: Harness) -> None:
    await registered(harness)
    run_id = harness.runs.add()
    task = harness.call(run_id, "call-3")
    await asyncio.wait_for(harness.publisher.published.wait(), 2)
    body = {"call_id": "call-3", "status": "succeeded", "content": "ok"}
    assert (await harness.post(run_id, body)).status_code == 204
    await asyncio.wait_for(task, 2)
    assert (await harness.post(run_id, body)).status_code == 204  # lost-response retry
    late = await harness.post(
        run_id, {"call_id": "never-sent", "status": "succeeded", "content": "x"}
    )
    assert late.status_code == 409
    error = late.json()["error"]
    assert error["code"] == "device_call_not_pending"
    registry = ERROR_CODES["device_call_not_pending"]
    assert (registry["http_status"], registry["public_message"]) == (409, error["message"])


async def test_result_must_match_the_run_and_the_owner(harness: Harness) -> None:
    await registered(harness)
    run_id = harness.runs.add()
    other_run = harness.runs.add()
    no_device_run = harness.runs.add(device=None)
    foreign_run = harness.runs.add(user=STRANGER)
    task = harness.call(run_id, "call-4")
    await asyncio.wait_for(harness.publisher.published.wait(), 2)
    body = {"call_id": "call-4", "status": "succeeded", "content": "x"}

    assert (await harness.post(other_run, body)).status_code == 409  # call belongs to another run
    assert (await harness.post(no_device_run, body)).status_code == 409
    assert (await harness.post(foreign_run, body)).status_code == 404  # not the caller's run
    assert (await harness.post(run_id, body, token="stranger")).status_code == 404
    assert (await harness.post(uuid.uuid4(), body)).status_code == 404
    assert not task.done()

    assert (await harness.post(run_id, body)).status_code == 204
    await asyncio.wait_for(task, 2)


@pytest.mark.parametrize(
    "body",
    [
        {"call_id": "c", "status": "succeeded"},
        {
            "call_id": "c",
            "status": "succeeded",
            "content": "x",
            "error": {"code": "timeout", "message": "m", "retryable": True},
        },
        {"call_id": "c", "status": "failed"},
        {
            "call_id": "c",
            "status": "uncertain",
            "error": {"code": "weird", "message": "m", "retryable": True},
        },
        {"call_id": "", "status": "succeeded", "content": "x"},
        {"call_id": "c", "status": "done", "content": "x"},
        {"call_id": "c", "status": "succeeded", "content": "x" * 8001},
    ],
)
async def test_result_body_is_validated(harness: Harness, body: dict[str, Any]) -> None:
    run_id = harness.runs.add()
    response = await harness.post(run_id, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_uncertain_write_result_is_delivered(harness: Harness) -> None:
    await harness.put(registration(declaration("device.alarm.set", risk="write_reversible")))
    run_id = harness.runs.add()
    task = harness.call(run_id, "call-5", tool="device.alarm.set")
    await asyncio.wait_for(harness.publisher.published.wait(), 2)
    body = {
        "call_id": "call-5",
        "status": "uncertain",
        "error": {
            "code": "timeout",
            "message": "The alarm app did not confirm.",
            "retryable": False,
        },
    }
    assert (await harness.post(run_id, body)).status_code == 204
    assert (await asyncio.wait_for(task, 2)).status == "uncertain"


async def test_android_client_registration_fixture_is_accepted(harness: Harness) -> None:
    """``fixtures/android_registration.json`` is the body ``:core:devicetools`` really sends.

    Regenerate with ``./gradlew :core:devicetools:testDebugUnitTest
    -PmiaDeviceToolsDump=<this file>`` in the Android repo."""
    body = json.loads(
        Path("tests/device_tools/fixtures/android_registration.json").read_text("utf-8")
    )
    assert_mvp_valid(body, "device-tools")
    device = uuid.UUID(body["device_id"])
    assert (await harness.put(body, device=device)).status_code == 204
    tools = await harness.channel.available_tools(user_id=str(OWNER), device_id=str(device))
    assert {tool.name for tool in tools} == {
        "device.contacts.search",
        "device.alarm.set",
        "device.app.open",
        "telegram.handoff",
    }
