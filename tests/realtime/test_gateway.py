from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid5

import pytest
from app.modules.realtime.contracts import (
    INPUT_AUDIO_FORMAT,
    OUTPUT_AUDIO_FORMAT,
    ConfigurePayload,
)
from app.modules.realtime.fake_provider import FakeRealtimeProvider
from app.modules.realtime.gateway import RealtimeGateway
from app.modules.realtime.provider import (
    ProviderAudio,
    ProviderControl,
    ProviderOutput,
    RealtimeProviderSession,
)
from app.modules.realtime.router import build_realtime_router
from app.modules.realtime.sessions import (
    AuthenticatedUser,
    InMemoryRealtimeSessionStore,
    RealtimeSession,
    RealtimeSessionStatus,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect
from tests.realtime.test_contracts import server_event_validator

USER_ID = UUID("90000000-0000-4000-8000-000000000001")
CONVERSATION_ID = UUID("20000000-0000-4000-8000-000000000001")
TURN_ID = UUID("40000000-0000-4000-8000-000000000001")
STREAM_ID = UUID("30000000-0000-4000-8000-000000000001")
AT = "2026-09-10T10:00:00Z"
TOKEN = "valid-test-token"


class StaticAuthenticator:
    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None:
        return AuthenticatedUser(USER_ID) if bearer_token == TOKEN else None


@dataclass(slots=True)
class Harness:
    client: TestClient
    store: InMemoryRealtimeSessionStore
    providers: list[RealtimeProviderSession]


def build_harness(
    *,
    provider_type: Callable[[RealtimeSession], RealtimeProviderSession] = FakeRealtimeProvider,
    input_queue_size: int = 8,
) -> Harness:
    store = InMemoryRealtimeSessionStore()
    authenticator = StaticAuthenticator()
    providers: list[RealtimeProviderSession] = []

    def provider_factory(session: RealtimeSession) -> RealtimeProviderSession:
        provider = provider_type(session)
        providers.append(provider)
        return provider

    gateway = RealtimeGateway(
        store=store,
        authenticator=authenticator,
        provider_factory=provider_factory,
        input_queue_size=input_queue_size,
    )
    app = FastAPI()
    app.include_router(
        build_realtime_router(
            store=store,
            authenticator=authenticator,
            gateway=gateway,
        )
    )
    return Harness(client=TestClient(app), store=store, providers=providers)


def create_session(client: TestClient, *, key: str = "realtime-session-key-001") -> str:
    response = client.post(
        "/v1/realtime/sessions",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": key},
        json={
            "conversation_id": str(CONVERSATION_ID),
            "locale": "ru-BY",
            "input_audio": INPUT_AUDIO_FORMAT.model_dump(mode="json"),
            "output_audio": OUTPUT_AUDIO_FORMAT.model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def configure(session_id: str, *, seq: int = 1) -> dict[str, object]:
    return {
        "v": 1,
        "type": "session.configure",
        "session_id": session_id,
        "seq": seq,
        "at": AT,
        "payload": {
            "protocol_version": 1,
            "conversation_id": str(CONVERSATION_ID),
            "locale": "ru-BY",
            "input_audio": [INPUT_AUDIO_FORMAT.model_dump(mode="json")],
            "output_audio": [OUTPUT_AUDIO_FORMAT.model_dump(mode="json")],
            "capabilities": ["barge_in", "input_transcript", "output_transcript"],
        },
    }


def start_input(session_id: str, *, seq: int = 2) -> dict[str, object]:
    return {
        "v": 1,
        "type": "input_audio.start",
        "session_id": session_id,
        "seq": seq,
        "at": AT,
        "payload": {
            "stream_id": str(STREAM_ID),
            "format": INPUT_AUDIO_FORMAT.model_dump(mode="json"),
        },
    }


def commit_input(session_id: str, *, seq: int = 3) -> dict[str, object]:
    return {
        "v": 1,
        "type": "input_audio.commit",
        "session_id": session_id,
        "turn_id": str(TURN_ID),
        "seq": seq,
        "at": AT,
        "payload": {
            "stream_id": str(STREAM_ID),
            "audio_bytes": 1280,
            "duration_ms": 40,
        },
    }


def close_session(session_id: str, *, seq: int) -> dict[str, object]:
    return {
        "v": 1,
        "type": "session.close",
        "session_id": session_id,
        "seq": seq,
        "at": AT,
        "payload": {"reason": "completed"},
    }


def receive_frame(websocket: WebSocketTestSession) -> dict[str, object] | bytes:
    message = websocket.receive()
    if message.get("bytes") is not None:
        return bytes(message["bytes"])
    return cast(dict[str, object], json.loads(str(message["text"])))


def test_session_creation_is_idempotent_and_requires_authentication() -> None:
    harness = build_harness()
    session_id = create_session(harness.client)
    assert create_session(harness.client) == session_id

    unauthorized = harness.client.post(
        "/v1/realtime/sessions",
        headers={"Idempotency-Key": "realtime-session-key-003"},
        json={
            "conversation_id": str(CONVERSATION_ID),
            "locale": "ru-BY",
            "input_audio": INPUT_AUDIO_FORMAT.model_dump(mode="json"),
            "output_audio": OUTPUT_AUDIO_FORMAT.model_dump(mode="json"),
        },
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "authentication_required"


def test_fake_handshake_binary_routing_and_schema_ordering() -> None:
    harness = build_harness()
    session_id = create_session(harness.client)
    controls: list[dict[str, object]] = []
    binaries: list[bytes] = []

    with harness.client.websocket_connect(
        f"/v1/realtime?session_id={session_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as websocket:
        websocket.send_json(configure(session_id))
        ready = cast(dict[str, object], websocket.receive_json())
        controls.append(ready)
        websocket.send_json(start_input(session_id))
        websocket.send_bytes(bytes(1280))
        websocket.send_json(commit_input(session_id))

        while True:
            frame = receive_frame(websocket)
            if isinstance(frame, bytes):
                binaries.append(frame)
                continue
            controls.append(frame)
            if frame["type"] == "usage.updated":
                break
        websocket.send_json(close_session(session_id, seq=4))

    validator = server_event_validator()
    for event in controls:
        validator.validate(event)
    assert [cast(int, event["seq"]) for event in controls] == list(range(1, len(controls) + 1))
    event_types = {str(event["type"]) for event in controls}
    assert {
        "session.ready",
        "transcript.input.final",
        "response.started",
        "response.audio.started",
        "response.audio.completed",
        "tool.call.started",
        "tool.call.completed",
        "turn.completed",
        "usage.updated",
    } <= event_types
    assert binaries == [bytes(index % 127 for index in range(1920))]


def test_configure_must_be_first_and_websocket_token_is_required() -> None:
    harness = build_harness()
    session_id = create_session(harness.client)
    with pytest.raises(WebSocketDisconnect) as denied:
        with harness.client.websocket_connect(f"/v1/realtime?session_id={session_id}"):
            pass
    assert denied.value.code == 4401

    second_session_id = create_session(harness.client, key="realtime-session-key-004")
    with harness.client.websocket_connect(
        f"/v1/realtime?session_id={second_session_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as websocket:
        websocket.send_json(
            {
                "v": 1,
                "type": "ping",
                "session_id": second_session_id,
                "seq": 1,
                "at": AT,
                "payload": {"nonce": "too-early"},
            }
        )
        error = websocket.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "realtime_invalid_event"


class SlowInputProvider(FakeRealtimeProvider):
    async def send_audio(self, chunk: bytes) -> None:
        await asyncio.sleep(10)


def test_input_backpressure_returns_controlled_protocol_error() -> None:
    harness = build_harness(provider_type=SlowInputProvider, input_queue_size=1)
    session_id = create_session(harness.client, key="realtime-session-key-005")
    with harness.client.websocket_connect(
        f"/v1/realtime?session_id={session_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as websocket:
        websocket.send_json(configure(session_id))
        _ = websocket.receive_json()
        websocket.send_json(start_input(session_id))
        websocket.send_bytes(bytes(1280))
        websocket.send_bytes(bytes(1280))
        websocket.send_bytes(bytes(1280))
        error = websocket.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "realtime_backpressure"
        server_event_validator().validate(error)


class CancellationProvider:
    _CLOSED = object()

    def __init__(self, session: RealtimeSession) -> None:
        self.session = session
        self.outputs: asyncio.Queue[ProviderOutput | object] = asyncio.Queue()
        self.turn_id: UUID | None = None

    async def configure(self, config: ConfigurePayload) -> None:
        return None

    async def send_audio(self, chunk: bytes) -> None:
        return None

    async def commit_audio(self, *, stream_id: UUID, turn_id: UUID) -> None:
        self.turn_id = turn_id
        response_id = FakeRealtimeProvider.response_id(turn_id)
        await self.outputs.put(
            ProviderControl(
                "response.started",
                {"modalities": ["audio"]},
                turn_id=turn_id,
                response_id=response_id,
            )
        )
        await self.outputs.put(
            ProviderControl(
                "response.audio.started",
                {
                    "segment_id": str(uuid5(response_id, "segment")),
                    "format": OUTPUT_AUDIO_FORMAT.model_dump(mode="json"),
                },
                turn_id=turn_id,
                response_id=response_id,
            )
        )

    async def cancel_response(self, response_id: UUID, reason: str) -> None:
        assert self.turn_id is not None
        await self.outputs.put(ProviderAudio(b"stale", response_id=response_id))
        await self.outputs.put(
            ProviderControl(
                "response.audio.completed",
                {
                    "segment_id": str(uuid5(response_id, "segment")),
                    "audio_bytes": 5,
                    "duration_ms": 1,
                },
                turn_id=self.turn_id,
                response_id=response_id,
            )
        )
        await self.outputs.put(
            ProviderControl(
                "response.interrupted",
                {"reason": "barge_in", "delivered_audio_ms": 0},
                turn_id=self.turn_id,
                response_id=response_id,
            )
        )

    async def events(self) -> AsyncIterator[ProviderOutput]:
        while True:
            event = await self.outputs.get()
            if event is self._CLOSED:
                return
            if isinstance(event, ProviderControl | ProviderAudio):
                yield event

    async def close(self) -> None:
        await self.outputs.put(self._CLOSED)


def test_cancel_filters_stale_audio_and_control_frames() -> None:
    harness = build_harness(provider_type=CancellationProvider)
    session_id = create_session(harness.client, key="realtime-session-key-006")
    response_id = FakeRealtimeProvider.response_id(TURN_ID)
    with harness.client.websocket_connect(
        f"/v1/realtime?session_id={session_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as websocket:
        websocket.send_json(configure(session_id))
        _ = websocket.receive_json()
        websocket.send_json(start_input(session_id))
        websocket.send_bytes(bytes(1280))
        websocket.send_json(commit_input(session_id))
        assert websocket.receive_json()["type"] == "response.started"
        assert websocket.receive_json()["type"] == "response.audio.started"
        websocket.send_json(
            {
                "v": 1,
                "type": "response.cancel",
                "session_id": session_id,
                "turn_id": str(TURN_ID),
                "response_id": str(response_id),
                "seq": 4,
                "at": AT,
                "payload": {"reason": "barge_in"},
            }
        )
        interrupted = receive_frame(websocket)
        assert isinstance(interrupted, dict)
        assert interrupted["type"] == "response.interrupted"
        server_event_validator().validate(interrupted)
        websocket.send_json(close_session(session_id, seq=5))


class TrackingProvider(CancellationProvider):
    def __init__(self, session: RealtimeSession) -> None:
        super().__init__(session)
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await super().close()


def test_disconnect_closes_provider_and_marks_session_interrupted() -> None:
    harness = build_harness(provider_type=TrackingProvider)
    session_id = create_session(harness.client, key="realtime-session-key-007")
    with harness.client.websocket_connect(
        f"/v1/realtime?session_id={session_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as websocket:
        websocket.send_json(configure(session_id))
        _ = websocket.receive_json()

    provider = cast(TrackingProvider, harness.providers[0])
    assert provider.closed is True
    session = asyncio.run(harness.store.get(UUID(session_id)))
    assert session is not None
    assert session.status is RealtimeSessionStatus.INTERRUPTED
