"""FCM data-only pushes to one device and the ``POST /v1/runs`` origin hook (M70)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.api.v1.runs import build_runs_router
from app.core.ports.approval_service import Approval
from app.core.ports.device_tool_channel import DeviceToolCall
from app.modules.notifications.device_push import call_message, consent_message
from app.modules.notifications.fcm import FcmClient
from app.modules.runs.domain import NewRun, RunRecord
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from tests.fakes import assert_mvp_valid
from tests.jobs.conftest import FakeFcm

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def approval() -> Approval:
    return Approval(
        id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        tool_call_id="call_c1",
        tool="device.contacts.search",
        plugin_id=None,
        risk="read",
        summary="Подтвердите на телефоне: браузер просит поиск в контактах «Яна».",
        arguments={"query": "Яна"},
        args_hash="sha256:" + "0" * 64,
        status="pending",
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
        device_consent={
            "capability": "contacts.read",
            "capability_label": "поиск в контактах телефона",
            "requester": {"kind": "web", "label": "браузер Chrome на macOS"},
            "model_sees": "только имена",
            "scopes": ["run", "persistent"],
        },
    )


async def test_consent_push_is_data_only_with_the_time_left() -> None:
    fcm = FakeFcm()
    client: FcmClient = fcm.client()
    message = consent_message(approval(), now=NOW + timedelta(seconds=20))
    assert_mvp_valid(message.data, "device-access", "DevicePushConsent")
    assert message.data["scopes"] == "run,persistent"
    assert (await client.send("token-1", message)).outcome == "sent"
    (sent,) = fcm.sent
    assert "notification" not in sent
    assert sent["android"] == {
        "priority": "HIGH",
        "ttl": "100s",
        "collapse_key": message.collapse_key,
    }
    assert "query" not in str(sent) and "Яна" not in sent["data"]["requester_label"]


def test_call_wake_up_carries_no_arguments() -> None:
    call = DeviceToolCall(
        call_id="call_c1",
        run_id=str(uuid.uuid4()),
        device_id=str(uuid.uuid4()),
        tool="device.contacts.search",
        arguments={"query": "Яна"},
        deadline_at=NOW,
    )
    message = call_message(call)
    assert_mvp_valid(message.data, "device-access", "DevicePushToolCall")
    assert message.data_only and "Яна" not in str(message.data)


class Service:
    """``RunService`` stand-in that records what it was asked to start."""

    def __init__(self) -> None:
        self.started: list[NewRun] = []

    async def start(self, new: NewRun, **_: Any) -> tuple[RunRecord, bool]:
        self.started.append(new)
        record = RunRecord(
            id=new.id,
            user_id=new.user_id,
            profile=new.profile,
            status="running",
            created_at=NOW,
            updated_at=NOW,
            device_id=new.device_id,
        )
        return record, False


class Bearer:
    def __init__(self, user: uuid.UUID) -> None:
        self.user = user

    async def authenticate(self, token: str) -> Any:
        from app.modules.realtime.sessions import AuthenticatedUser

        return AuthenticatedUser(user_id=self.user)


class Origins:
    def __init__(self, device: uuid.UUID | None) -> None:
        self.device = device
        self.committed: list[tuple[uuid.UUID, bool]] = []

    async def prepare(self, request: Request, new: NewRun) -> NewRun:
        return NewRun(
            user_id=new.user_id,
            profile=new.profile,
            input_text=new.input_text,
            device_id=self.device,
            id=new.id,
        )

    async def commit(self, record: RunRecord, *, replayed: bool) -> None:
        self.committed.append((record.id, replayed))


async def test_runs_api_uses_the_verified_device_and_records_the_origin() -> None:
    user, verified = uuid.uuid4(), uuid.uuid4()
    service = Service()
    app = FastAPI()
    app.include_router(build_runs_router(service=service, authenticator=Bearer(user)))  # type: ignore[arg-type]
    origins = Origins(verified)
    app.state.run_origins = origins
    async with AsyncClient(transport=ASGITransport(app), base_url="http://mia.test") as client:
        response = await client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer x", "Idempotency-Key": "k" * 16},
            json={
                "profile": "chat",
                "input": {"text": "найди Яну"},
                "device_id": str(uuid.uuid4()),
            },
        )
    assert response.status_code == 202, response.text
    (started,) = service.started
    assert started.device_id == verified
    assert origins.committed == [(started.id, False)]
