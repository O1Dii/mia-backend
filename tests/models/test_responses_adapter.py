"""Record/replay tests of the primary adapter (chatgpt-plan, Responses API)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from app.core.ports.model_gateway import (
    ModelDone,
    ModelGatewayError,
    ModelMessage,
    ModelToolCall,
    ModelUsage,
    TextDelta,
    ToolCallRequested,
)
from app.modules.models.adapters import AdapterSettings
from app.modules.models.errors import ProviderError

from tests.models.helpers import (
    TOOLS,
    FakeTokens,
    assistant_with,
    collect,
    make_adapter,
    make_request,
)
from tests.models.replay import ReplayTransport

PLAN = "chatgpt-plan"


def settings(**kwargs: Any) -> AdapterSettings:
    async def no_sleep(_s: float) -> None:
        return None

    return AdapterSettings(sleep=no_sleep, **kwargs)


async def test_text_answer_streams_deltas_usage_and_done() -> None:
    transport = ReplayTransport("plan_text")
    tokens = FakeTokens()
    adapter, client = make_adapter(PLAN, transport, settings(), model="gpt-6.1-sol", tokens=tokens)
    events = await collect(
        adapter,
        make_request(ModelMessage("system", "Be brief."), ModelMessage("user", "Say hello")),
    )
    await client.aclose()

    assert events == [
        TextDelta("Hello, "),
        TextDelta("world!"),
        ModelUsage(input_tokens=12, output_tokens=7),
        ModelDone("stop"),
    ]
    body = transport.bodies[0]
    assert body["model"] == "gpt-6.1-sol"
    assert body["store"] is False and body["stream"] is True
    assert body["instructions"] == "Be brief."
    assert body["input"] == [{"role": "user", "content": "Say hello"}]
    assert "tools" not in body
    assert transport.requests[0].headers["authorization"] == "Bearer plan-access-1"


async def test_plan_mode_never_sends_parameters_the_plan_rejects() -> None:
    transport = ReplayTransport("plan_text")
    adapter, client = make_adapter(PLAN, transport, settings(), tokens=FakeTokens())
    await collect(adapter, make_request(max_output_tokens=50))
    await client.aclose()
    assert "max_output_tokens" not in transport.bodies[0]  # cassette also forbids the others


async def test_tool_loop_continues_without_ids_and_reasoning_items() -> None:
    """M01 scenario: get_time -> add_todo -> final text; continuation in the verified plain mode."""
    transport = ReplayTransport("plan_tool_loop")
    adapter, client = make_adapter(PLAN, transport, settings(), tokens=FakeTokens())
    messages = [ModelMessage("user", "Call Anna in an hour")]
    final_text = ""
    for step in range(3):
        events = await collect(adapter, make_request(*messages, tools=TOOLS))
        calls = [e.call for e in events if isinstance(e, ToolCallRequested)]
        if not calls:
            final_text = "".join(e.text for e in events if isinstance(e, TextDelta))
            assert events[-1] == ModelDone("stop")
            break
        assert events[-1] == ModelDone("tool_calls")
        call = calls[0]
        messages.append(assistant_with(call))
        messages.append(ModelMessage("tool", json.dumps({"ok": step}), tool_call_id=call.call_id))
    await client.aclose()

    assert final_text == "Done: todo added."
    assert transport.remaining == 0
    first, second, third = transport.bodies
    assert first["tools"][0] == {
        "type": "function",
        "name": "get_time",
        "description": "Return the current time in an IANA timezone.",
        "parameters": TOOLS[0].input_schema,
    }
    assert first["tool_choice"] == "auto"
    assert second["input"][1:] == [
        {
            "type": "function_call",
            "call_id": "call_a1",
            "name": "get_time",
            "arguments": '{"timezone": "UTC"}',
        },
        {"type": "function_call_output", "call_id": "call_a1", "output": '{"ok": 0}'},
    ]
    assert all("id" not in i for i in third["input"] if i.get("type") == "function_call")
    assert [i["call_id"] for i in third["input"] if i.get("type") == "function_call"] == [
        "call_a1",
        "call_b2",
    ]


async def test_encrypted_reasoning_is_kept_in_provider_state_and_replayed_verbatim() -> None:
    transport = ReplayTransport("plan_reasoning_state", "plan_text")
    adapter, client = make_adapter(PLAN, transport, settings(), tokens=FakeTokens())
    events = await collect(adapter, make_request(tools=TOOLS))
    call = next(e.call for e in events if isinstance(e, ToolCallRequested))
    assert call.provider_state == {
        "responses_reasoning_items": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": "ENC-BLOB-1",
            }
        ]
    }
    # the persisted/JSON-roundtripped state comes back on the next request unchanged
    stored = ModelToolCall(
        call.call_id, call.name, call.arguments, json.loads(json.dumps(call.provider_state))
    )
    await collect(
        adapter,
        make_request(
            ModelMessage("user", "hi"),
            assistant_with(stored),
            ModelMessage("tool", "{}", tool_call_id=call.call_id),
            tools=TOOLS,
        ),
    )
    await client.aclose()
    items = transport.bodies[1]["input"]
    assert items[1] == {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "ENC-BLOB-1",
    }
    assert items[2]["type"] == "function_call" and "id" not in items[2]
    assert not any(i.get("id") == "rs_2" for i in items)  # no encrypted_content -> dropped


async def test_parallel_calls_arrive_in_order() -> None:
    transport = ReplayTransport("plan_parallel_calls")
    adapter, client = make_adapter(PLAN, transport, settings(), tokens=FakeTokens())
    events = await collect(adapter, make_request(tools=TOOLS))
    await client.aclose()
    assert [e.call.call_id for e in events if isinstance(e, ToolCallRequested)] == [
        "call_p1",
        "call_p2",
    ]
    assert events[-1] == ModelDone("tool_calls")


async def test_length_limited_answer_reports_length() -> None:
    adapter, client = make_adapter(
        PLAN, ReplayTransport("plan_incomplete_length"), settings(), tokens=FakeTokens()
    )
    events = await collect(adapter, make_request())
    await client.aclose()
    assert events[-1] == ModelDone("length")
    assert events[-2] == ModelUsage(5, 100)


@pytest.mark.parametrize(
    ("cassette", "code", "retryable", "usage_limit", "reauth"),
    [
        ("plan_usage_limit_http", "rate_limited", False, True, False),
        ("plan_usage_limit_stream", "rate_limited", False, True, False),
        ("plan_context_too_long", "context_too_long", False, False, False),
        ("plan_unsupported_capability", "invalid_request", False, False, False),
        ("plan_not_eligible", "model_not_configured", False, False, False),
        ("plan_401_always", "model_not_configured", False, False, True),
        ("plan_truncated", "provider_unavailable", True, False, False),
        ("plan_bad_arguments", "invalid_request", True, False, False),
        ("plan_503_always", "provider_unavailable", True, False, False),
    ],
)
async def test_errors_are_mapped_and_content_free(
    cassette: str, code: str, retryable: bool, usage_limit: bool, reauth: bool
) -> None:
    transport = ReplayTransport(cassette)
    adapter, client = make_adapter(PLAN, transport, settings(), tokens=FakeTokens())
    with pytest.raises(ProviderError) as caught:
        await collect(adapter, make_request(tools=TOOLS))
    await client.aclose()
    error = caught.value
    assert (error.code, error.retryable, error.usage_limit, error.needs_reauth) == (
        code,
        retryable,
        usage_limit,
        reauth,
    )
    for leaked in (
        "UPSTREAM-LIMIT-TEXT",
        "UPSTREAM-BOOM-TEXT",
        "UPSTREAM-CONTEXT-TEXT",
        "UPSTREAM-401-TEXT",
    ):
        assert leaked not in str(error)
    assert isinstance(error, ModelGatewayError)


async def test_usage_limit_carries_retry_after_or_default_pause() -> None:
    adapter, client = make_adapter(
        PLAN, ReplayTransport("plan_usage_limit_http"), settings(), tokens=FakeTokens()
    )
    with pytest.raises(ProviderError) as http_error:
        await collect(adapter, make_request())
    assert http_error.value.pause_seconds == 120
    assert http_error.value.provider_code == "subscription_sharing_usage_limit_exceeded"
    assert http_error.value.switchable

    adapter, client = make_adapter(
        PLAN,
        ReplayTransport("plan_usage_limit_stream"),
        settings(pause_seconds=900),
        tokens=FakeTokens(),
    )
    with pytest.raises(ProviderError) as stream_error:
        await collect(adapter, make_request())
    assert stream_error.value.pause_seconds == 900
    await client.aclose()


async def test_midstream_failure_surfaces_after_partial_text() -> None:
    adapter, client = make_adapter(
        PLAN, ReplayTransport("plan_failed_midstream"), settings(), tokens=FakeTokens()
    )
    seen: list[Any] = []
    with pytest.raises(ProviderError) as caught:
        async for event in adapter.stream(make_request()):
            seen.append(event)
    await client.aclose()
    assert seen == [TextDelta("partial")]
    assert caught.value.code == "provider_unavailable" and caught.value.retryable


async def test_503_is_retried_with_bounded_backoff() -> None:
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    transport = ReplayTransport("plan_503_then_ok")
    adapter, client = make_adapter(
        PLAN, transport, AdapterSettings(sleep=record, backoff_seconds=0.5), tokens=FakeTokens()
    )
    events = await collect(adapter, make_request())
    await client.aclose()
    assert events[-1] == ModelDone("stop")
    assert delays == [0.5]
    assert transport.remaining == 0


async def test_401_refreshes_once_then_retries_with_a_new_token() -> None:
    transport = ReplayTransport("plan_401_then_ok")
    tokens = FakeTokens()
    adapter, client = make_adapter(PLAN, transport, settings(), tokens=tokens)
    events = await collect(adapter, make_request())
    await client.aclose()
    assert events[-1] == ModelDone("stop")
    assert tokens.invalidated == 1
    assert [r.headers["authorization"] for r in transport.requests] == [
        "Bearer plan-access-1",
        "Bearer plan-access-2",
    ]


async def test_cancel_closes_the_upstream_connection_before_the_stream_ends() -> None:
    transport = ReplayTransport("plan_text")
    adapter, client = make_adapter(PLAN, transport, settings(), tokens=FakeTokens())
    stream = adapter.stream(make_request())
    first = await anext(stream)
    assert first == TextDelta("Hello, ")
    await stream.aclose()  # the run engine cancels the step
    await client.aclose()
    assert transport.streams[0].closed
    assert transport.streams[0].sent < 12  # the rest of the body was never pulled


class _HangingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


async def test_task_cancellation_while_waiting_for_the_provider_closes_the_connection() -> None:
    hanging = _HangingStream()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=hanging)

    adapter, client = make_adapter(
        PLAN, httpx.MockTransport(handler), settings(), tokens=FakeTokens()
    )

    async def consume() -> None:
        async for _event in adapter.stream(make_request()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(hanging.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()
    assert hanging.closed


async def test_stream_deadline_maps_to_provider_timeout() -> None:
    ticks = iter(range(0, 1000, 100))
    adapter, client = make_adapter(
        PLAN,
        ReplayTransport("plan_text"),
        settings(stream_deadline_seconds=150, clock=lambda: float(next(ticks))),
        tokens=FakeTokens(),
    )
    with pytest.raises(ProviderError) as caught:
        await collect(adapter, make_request())
    await client.aclose()
    assert caught.value.code == "provider_timeout" and caught.value.retryable


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (httpx.ReadTimeout("slow"), "provider_timeout"),
        (httpx.ConnectTimeout("slow"), "provider_timeout"),
        (httpx.ConnectError("refused"), "provider_unavailable"),
        (httpx.RemoteProtocolError("reset"), "provider_unavailable"),
    ],
)
async def test_transport_failures_are_mapped(raised: Exception, code: str) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise raised

    adapter, client = make_adapter(
        PLAN, httpx.MockTransport(handler), settings(), tokens=FakeTokens()
    )
    with pytest.raises(ProviderError) as caught:
        await collect(adapter, make_request())
    await client.aclose()
    assert caught.value.code == code and caught.value.retryable
    assert calls == (3 if isinstance(raised, httpx.ConnectError) else 1)
    assert "refused" not in str(caught.value) and "slow" not in str(caught.value)
