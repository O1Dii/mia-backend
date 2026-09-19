"""One registry, one policy and one audit trail across both modalities."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid5

import pytest
from app.modules.realtime.provider import ProviderControl, ProviderOutput
from fastapi.testclient import TestClient

from tests.e2e_tools import harness

pytestmark = [harness.requires_backend, pytest.mark.usefixtures("backend")]

UNREGISTERED = "system.delete_everything"


def _script(turn_id: UUID, response_id: UUID) -> Sequence[ProviderOutput]:
    return (
        ProviderControl(
            "response.started",
            {"modalities": ["audio"]},
            turn_id=turn_id,
            response_id=response_id,
        ),
        ProviderControl(
            "tool.call.started",
            {
                "call_id": str(uuid5(response_id, "scripted-tool-call")),
                "name": UNREGISTERED,
                "arguments": {"timezone": "Europe/Minsk"},
                "requires_confirmation": False,
            },
            turn_id=turn_id,
            response_id=response_id,
        ),
        ProviderControl("turn.completed", {"status": "completed"}, turn_id=turn_id),
    )


def test_an_unregistered_tool_is_denied_and_audited_identically_in_both_modalities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_provider = harness.ScriptedTextProvider(
        harness.ScriptedToolCall(name=UNREGISTERED, arguments={"timezone": "Europe/Minsk"})
    )
    realtime_factory = harness.ScriptedRealtimeFactory(_script)
    registry = harness.frozen_registry()
    composed = harness.compose(
        monkeypatch,
        text_provider=text_provider,
        realtime_provider=realtime_factory,
        registry=registry,
    )

    with TestClient(composed.app) as client:
        conversation_id = harness.new_conversation(
            client, composed, key=harness.fresh_key("conversation")
        )
        streamed = client.post(
            f"/v1/conversations/{conversation_id}/messages:stream",
            headers=composed.headers(harness.fresh_key("turn")),
            json={"text": "Удали всё"},
        )
        assert streamed.status_code == 200

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
            frames: list[dict[str, Any]] = []
            for _ in range(20):
                frame = harness.receive_frame(websocket)
                if isinstance(frame, dict):
                    frames.append(frame)
                    if frame["type"] == "tool.call.completed":
                        break
            websocket.send_json(harness.close_event(session_id, seq=4))

    text_completed = harness.payload_of(
        next(
            event
            for event in harness.sse_events(streamed.text)
            if event["name"] == "tool.call.completed"
        )
    )
    realtime_completed = next(
        frame["payload"] for frame in frames if frame["type"] == "tool.call.completed"
    )
    for payload in (text_completed, realtime_completed):
        assert payload["name"] == UNREGISTERED
        assert payload["status"] == "failed"
        assert payload["error"]["code"] == "tool_not_found"
        assert "result" not in payload

    # One executor and one registry served both modalities.
    assert len(composed.executors) == 1
    assert composed.executors[0].registry is registry
    assert [tool.name for tool in text_provider.requests[0].tools] == [harness.TOOL_NAME]

    rows = harness.audit_rows_blocking()
    assert len(rows) == 2
    assert {row.name for row in rows} == {UNREGISTERED}
    assert {row.status for row in rows} == {"failed"}
    assert {row.error_code for row in rows} == {"tool_not_found"}
    assert {row.user_id for row in rows} == {harness.USER_ID}
    assert {row.result for row in rows} == {None}
    assert len({row.call_id for row in rows}) == 2
    assert {str(row.call_id) for row in rows} == {
        text_completed["call_id"],
        realtime_completed["call_id"],
    }
