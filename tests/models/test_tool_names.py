"""OI-M13-001: router tool names reach OpenAI-compatible APIs as valid aliases and come back."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from app.core.ports.model_gateway import (
    ModelMessage,
    ModelToolCall,
    ToolCallRequested,
    ToolSpec,
)
from app.modules.models.adapters import AdapterSettings
from app.modules.models.adapters.tool_names import ToolNames

from tests.models.helpers import FakeTokens, assistant_with, collect, make_adapter, make_request

WIRE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
SCHEMA = {"type": "object", "properties": {"seconds": {"type": "integer"}}}
TOOLS = (
    ToolSpec("builtin.timer.start", "Start a timer.", SCHEMA),
    ToolSpec("builtin.timer.get", "Read timers.", SCHEMA),
    ToolSpec("get_time", "Current time.", SCHEMA),
)


async def _no_sleep(_s: float) -> None:
    return None


def test_aliases_are_valid_unique_stable_and_reversible() -> None:
    names = {
        "builtin.timer.start",
        "builtin_timer_start",
        "builtin__timer__start",
        "get_time",
        "x." + "a" * 80,
        "x." + "a" * 80 + ".b",
        "плагин.инструмент",
    }
    first = ToolNames(names)
    second = ToolNames(set(reversed(sorted(names))))
    aliases = {first.wire(n) for n in names}
    assert len(aliases) == len(names)
    for name in names:
        assert WIRE.fullmatch(first.wire(name))
        assert first.canonical(first.wire(name)) == name
        assert second.wire(name) == first.wire(name)
    # Already valid names are sent unchanged and win collisions.
    assert first.wire("get_time") == "get_time"
    assert first.wire("builtin__timer__start") == "builtin__timer__start"
    assert first.canonical("unknown") == "unknown"


def _sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events)


async def test_responses_adapter_aliases_tools_history_and_maps_calls_back() -> None:
    bodies: list[dict[str, Any]] = []
    wire = ToolNames({t.name for t in TOOLS}).wire("builtin.timer.start")

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        item = {"type": "function_call", "call_id": "c2", "name": wire, "arguments": "{}"}
        payload = _sse(
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {}},
        )
        return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})

    adapter, client = make_adapter(
        "chatgpt-plan",
        httpx.MockTransport(handler),
        AdapterSettings(sleep=_no_sleep),
        tokens=FakeTokens(),
    )
    previous = ModelToolCall("c1", "builtin.timer.get", {})
    request = make_request(
        ModelMessage("user", "timer"),
        assistant_with(previous),
        ModelMessage("tool", "none", tool_call_id="c1"),
        tools=TOOLS,
    )
    events = await collect(adapter, request)
    await client.aclose()

    body = bodies[0]
    assert all(WIRE.fullmatch(tool["name"]) for tool in body["tools"])
    replayed = [i for i in body["input"] if i.get("type") == "function_call"]
    assert replayed[0]["name"] == ToolNames({t.name for t in TOOLS}).wire("builtin.timer.get")
    calls = [e.call for e in events if isinstance(e, ToolCallRequested)]
    assert [c.name for c in calls] == ["builtin.timer.start"]


async def test_chat_completions_adapter_aliases_tools_and_maps_calls_back() -> None:
    bodies: list[dict[str, Any]] = []
    wire = ToolNames({t.name for t in TOOLS}).wire("builtin.timer.start")

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        call = {"index": 0, "id": "c1", "function": {"name": wire, "arguments": "{}"}}
        chunk = {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}
        payload = _sse(chunk) + b"data: [DONE]\n\n"
        return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})

    adapter, client = make_adapter(
        "deepinfra",
        httpx.MockTransport(handler),
        AdapterSettings(sleep=_no_sleep),
        api_key="test-key",
    )
    events = await collect(adapter, make_request(ModelMessage("user", "timer"), tools=TOOLS))
    await client.aclose()

    names = [tool["function"]["name"] for tool in bodies[0]["tools"]]
    assert all(WIRE.fullmatch(name) for name in names) and "get_time" in names
    calls = [e.call for e in events if isinstance(e, ToolCallRequested)]
    assert [c.name for c in calls] == ["builtin.timer.start"]
