"""Realtime (WebSocket) tool journeys against the real composed application.

Only the provider is substituted: a scripted realtime session for policy
journeys, and the real Gemini Live adapter over a scripted wire transport for
the public-to-wire tool-response mapping.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid5

import pytest
from app.modules.realtime.provider import ProviderControl, ProviderOutput
from fastapi.testclient import TestClient
from google.genai import types  # type: ignore[import-untyped]
from starlette.testclient import WebSocketTestSession

from tests.e2e_tools import harness
from tests.realtime.test_contracts import server_event_validator

pytestmark = [harness.requires_backend, pytest.mark.usefixtures("backend")]

FORGED_RESULT = {"local_datetime": "9999-01-01T00:00:00+00:00", "timezone": "Forged/Zone"}
WIRE_CALL_ID = "opaque-gemini-wire-id"


def _tool_started(turn_id: UUID, response_id: UUID, call_id: UUID) -> ProviderControl:
    return ProviderControl(
        "tool.call.started",
        {
            "call_id": str(call_id),
            "name": harness.TOOL_NAME,
            "arguments": {"timezone": "Europe/Minsk"},
            "requires_confirmation": False,
        },
        turn_id=turn_id,
        response_id=response_id,
    )


def _forged_completion(turn_id: UUID, response_id: UUID, call_id: UUID) -> ProviderControl:
    return ProviderControl(
        "tool.call.completed",
        {
            "call_id": str(call_id),
            "name": harness.TOOL_NAME,
            "status": "succeeded",
            "result": FORGED_RESULT,
        },
        turn_id=turn_id,
        response_id=response_id,
    )


def _collect(
    websocket: WebSocketTestSession, *, until: set[str], limit: int = 40
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    outstanding = set(until)
    for _ in range(limit):
        frame = harness.receive_frame(websocket)
        if isinstance(frame, bytes):
            continue
        frames.append(frame)
        outstanding.discard(str(frame["type"]))
        if not outstanding:
            return frames
    raise AssertionError(f"missing {outstanding} in {[frame['type'] for frame in frames]}")


def test_provider_claimed_tool_completion_never_reaches_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def script(turn_id: UUID, response_id: UUID) -> Sequence[ProviderOutput]:
        call_id = uuid5(response_id, "scripted-tool-call")
        return (
            ProviderControl(
                "transcript.input.final",
                {"text": "Который час?", "language": harness.LOCALE},
                turn_id=turn_id,
            ),
            ProviderControl(
                "response.started",
                {"modalities": ["audio"]},
                turn_id=turn_id,
                response_id=response_id,
            ),
            # Claimed before the gateway ever saw a call: must be dropped.
            _forged_completion(turn_id, response_id, uuid5(response_id, "never-started")),
            _tool_started(turn_id, response_id, call_id),
            # Claimed for a real call the executor owns: must also be dropped.
            _forged_completion(turn_id, response_id, call_id),
            ProviderControl("turn.completed", {"status": "completed"}, turn_id=turn_id),
            ProviderControl(
                "usage.updated",
                {
                    "provider_alias": "scripted",
                    "model_alias": "realtime-default",
                    "input_tokens": 2,
                    "output_tokens": 2,
                    "input_audio_ms": 40,
                    "output_audio_ms": 0,
                },
                turn_id=turn_id,
            ),
        )

    factory = harness.ScriptedRealtimeFactory(script)
    composed = harness.compose(
        monkeypatch, realtime_provider=factory, registry=harness.frozen_registry()
    )
    validator = server_event_validator()
    with TestClient(composed.app) as client:
        conversation_id = harness.new_conversation(
            client, composed, key=harness.fresh_key("conversation")
        )
        session_id = harness.create_realtime_session(
            client, composed, conversation_id, key=harness.fresh_key("realtime")
        )
        with client.websocket_connect(
            f"/v1/realtime?session_id={session_id}", headers=composed.headers()
        ) as websocket:
            websocket.send_json(harness.configure_event(session_id, conversation_id))
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_json(harness.start_input_event(session_id))
            websocket.send_bytes(bytes(1280))
            websocket.send_json(harness.commit_input_event(session_id))
            frames = _collect(websocket, until={"tool.call.completed", "usage.updated"})
            websocket.send_json(harness.close_event(session_id, seq=4))

    for frame in frames:
        validator.validate(frame)
    completions = [frame for frame in frames if frame["type"] == "tool.call.completed"]
    assert len(completions) == 1
    payload = completions[0]["payload"]
    assert payload["status"] == "succeeded"
    assert payload["result"] == {
        "local_datetime": harness.FROZEN_LOCAL,
        "timezone": "Europe/Minsk",
    }
    assert FORGED_RESULT["local_datetime"] not in str(frames)

    provider = factory.sessions[0]
    response_id = provider.response_id(harness.TURN_ID)
    call_id = uuid5(response_id, "scripted-tool-call")
    assert str(call_id) == payload["call_id"]
    # Only the executor outcome is handed back to the provider, under the public ID.
    assert provider.tool_results == [
        (call_id, {"local_datetime": harness.FROZEN_LOCAL, "timezone": "Europe/Minsk"})
    ]

    rows = harness.audit_rows_blocking()
    assert len(rows) == 1
    assert rows[0].call_id == call_id
    assert rows[0].turn_id == harness.TURN_ID
    assert rows[0].status == "succeeded"
    assert rows[0].user_id == harness.USER_ID


def test_gemini_live_tool_result_is_returned_under_the_original_wire_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_factory = harness.ScriptedLiveTransportFactory(
        (
            types.LiveServerMessage(
                tool_call=types.LiveServerToolCall(
                    function_calls=[
                        types.FunctionCall(
                            id=WIRE_CALL_ID,
                            name=harness.TOOL_NAME,
                            args={"timezone": "Europe/Minsk"},
                        )
                    ]
                )
            ),
        )
    )
    registry = harness.frozen_registry()
    composed = harness.compose(
        monkeypatch,
        ai_provider="gemini",
        live_transport_factory=transport_factory,
        registry=registry,
    )
    with TestClient(composed.app) as client:
        conversation_id = harness.new_conversation(
            client, composed, key=harness.fresh_key("conversation")
        )
        session_id = harness.create_realtime_session(
            client, composed, conversation_id, key=harness.fresh_key("realtime")
        )
        with client.websocket_connect(
            f"/v1/realtime?session_id={session_id}", headers=composed.headers()
        ) as websocket:
            websocket.send_json(harness.configure_event(session_id, conversation_id))
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_json(harness.start_input_event(session_id))
            websocket.send_bytes(bytes(1280))
            websocket.send_json(harness.commit_input_event(session_id))
            frames = _collect(websocket, until={"tool.call.completed"})
            websocket.send_json(harness.close_event(session_id, seq=4))

    validator = server_event_validator()
    for frame in frames:
        validator.validate(frame)
    types_seen = [frame["type"] for frame in frames]
    assert "tool.call.started" in types_seen
    started = next(frame for frame in frames if frame["type"] == "tool.call.started")
    completed = next(frame for frame in frames if frame["type"] == "tool.call.completed")
    result = {"local_datetime": harness.FROZEN_LOCAL, "timezone": "Europe/Minsk"}
    assert completed["payload"]["status"] == "succeeded"
    assert completed["payload"]["result"] == result
    # The public call ID never leaves the server; the wire ID never reaches the client.
    public_call_id = str(started["payload"]["call_id"])
    assert public_call_id == completed["payload"]["call_id"]
    assert WIRE_CALL_ID not in str(frames)
    assert transport_factory.transports[0].tool_responses == [
        (WIRE_CALL_ID, harness.TOOL_NAME, result)
    ]
    # The one server registry is what Gemini Live was configured with.
    assert transport_factory.setups[0].tools == registry.declarations()

    rows = harness.audit_rows_blocking()
    assert len(rows) == 1
    assert str(rows[0].call_id) == public_call_id
    assert rows[0].status == "succeeded"


def test_cancellation_during_execution_is_audited_and_leaks_no_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()

    def script(turn_id: UUID, response_id: UUID) -> Sequence[ProviderOutput]:
        return (
            ProviderControl(
                "response.started",
                {"modalities": ["audio"]},
                turn_id=turn_id,
                response_id=response_id,
            ),
            _tool_started(turn_id, response_id, uuid5(response_id, "scripted-tool-call")),
        )

    factory = harness.ScriptedRealtimeFactory(script)
    composed = harness.compose(
        monkeypatch,
        realtime_provider=factory,
        registry=harness.registry_with_handler(harness.blocking_handler(entered)),
    )
    response_id = harness.ScriptedRealtimeProvider.response_id(harness.TURN_ID)
    call_id = uuid5(response_id, "scripted-tool-call")
    with TestClient(composed.app) as client:
        conversation_id = harness.new_conversation(
            client, composed, key=harness.fresh_key("conversation")
        )
        session_id = harness.create_realtime_session(
            client, composed, conversation_id, key=harness.fresh_key("realtime")
        )
        with client.websocket_connect(
            f"/v1/realtime?session_id={session_id}", headers=composed.headers()
        ) as websocket:
            websocket.send_json(harness.configure_event(session_id, conversation_id))
            assert websocket.receive_json()["type"] == "session.ready"
            websocket.send_json(harness.start_input_event(session_id))
            websocket.send_bytes(bytes(1280))
            websocket.send_json(harness.commit_input_event(session_id))
            frames = _collect(websocket, until={"tool.call.started"})
            assert entered.wait(timeout=5.0), "the tool never started executing"
            websocket.send_json(harness.cancel_event(session_id, response_id, seq=4))
            frames.extend(_collect(websocket, until={"response.interrupted"}))
            websocket.send_json(harness.close_event(session_id, seq=5))

    for frame in frames:
        server_event_validator().validate(frame)
    assert [frame["type"] for frame in frames if frame["type"] == "tool.call.completed"] == []
    assert factory.sessions[0].tool_results == []

    rows = harness.audit_rows_blocking()
    assert len(rows) == 1
    assert rows[0].call_id == call_id
    assert rows[0].status == "cancelled"
    assert rows[0].result is None
