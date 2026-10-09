"""Replay tests for the OpenAI-compatible fallback (DeepInfra GLM) and the chat_only ports."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from app.core.ports.model_gateway import (
    ModelDone,
    ModelMessage,
    ModelUsage,
    TextDelta,
    ToolCallRequested,
)
from app.modules.models.adapters import AdapterSettings
from app.modules.models.errors import ProviderError

from tests.models.helpers import TOOLS, assistant_with, collect, make_adapter, make_request
from tests.models.replay import ReplayTransport

GLM = "zai-org/GLM-5.3-Flash"
KEY = "sk-test-DEEPINFRA-0123456789abcdef"


def quiet() -> AdapterSettings:
    async def no_sleep(_s: float) -> None:
        return None

    return AdapterSettings(sleep=no_sleep)


async def test_glm_text_with_cost_from_provider_and_hidden_reasoning() -> None:
    transport = ReplayTransport("glm_text")
    adapter, client = make_adapter("deepinfra", transport, quiet(), model=GLM, api_key=KEY)
    events = await collect(
        adapter, make_request(ModelMessage("system", "s"), ModelMessage("user", "hi"))
    )
    await client.aclose()
    assert [e for e in events if isinstance(e, TextDelta)] == [
        TextDelta("Hello, "),
        TextDelta("world!"),
    ]
    assert all("short thought" not in repr(e) for e in events)  # reasoning is never a client event
    usage = next(e for e in events if isinstance(e, ModelUsage))
    assert (usage.input_tokens, usage.output_tokens) == (120, 9)
    assert usage.cost_usd == pytest.approx((120 * 0.15 + 9 * 0.5) / 1_000_000)
    assert events[-1] == ModelDone("stop")
    body = transport.bodies[0]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
    ]
    assert transport.requests[0].headers["authorization"] == f"Bearer {KEY}"


async def test_glm_tool_loop_replays_reasoning_content_from_provider_state() -> None:
    transport = ReplayTransport("glm_tool_loop")
    adapter, client = make_adapter("deepinfra", transport, quiet(), model=GLM, api_key=KEY)
    messages = [ModelMessage("user", "Call Anna")]
    final = ""
    for _ in range(3):
        events = await collect(adapter, make_request(*messages, tools=TOOLS))
        calls = [e.call for e in events if isinstance(e, ToolCallRequested)]
        if not calls:
            final = "".join(e.text for e in events if isinstance(e, TextDelta))
            break
        call = calls[0]
        assert call.provider_state is not None
        # state survives persistence by the run engine
        stored = type(call)(
            call.call_id, call.name, call.arguments, json.loads(json.dumps(call.provider_state))
        )
        messages.append(assistant_with(stored))
        messages.append(ModelMessage("tool", "{}", tool_call_id=call.call_id))
    await client.aclose()

    assert final == "Done, todo added."
    assert transport.remaining == 0
    second = transport.bodies[1]["messages"]
    assistant = second[1]
    assert assistant["reasoning_content"] == "I should call get_time first."
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"timezone": "UTC"}'
    assert second[2] == {"role": "tool", "tool_call_id": "t1", "content": "{}"}
    assert transport.bodies[0]["tools"][0]["function"]["name"] == "get_time"
    third_assistants = [m for m in transport.bodies[2]["messages"] if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in third_assistants] == [
        "I should call get_time first.",
        "Now add the todo item.",
    ]


async def test_glm_tool_call_event_shape_and_finish_reason() -> None:
    adapter, client = make_adapter(
        "deepinfra", ReplayTransport("glm_tool_loop"), quiet(), model=GLM, api_key=KEY
    )
    events = await collect(adapter, make_request(tools=TOOLS))
    await client.aclose()
    call = next(e.call for e in events if isinstance(e, ToolCallRequested))
    assert (call.call_id, call.name, dict(call.arguments)) == (
        "t1",
        "get_time",
        {"timezone": "UTC"},
    )
    assert events[-1] == ModelDone("tool_calls")
    usage = next(e for e in events if isinstance(e, ModelUsage))
    assert usage.cost_usd == pytest.approx(0.000025)


async def test_glm_errors_are_content_free() -> None:
    adapter, client = make_adapter(
        "deepinfra", ReplayTransport("glm_rate_limited"), quiet(), model=GLM, api_key=KEY
    )
    with pytest.raises(ProviderError) as limited:
        await collect(adapter, make_request())
    assert limited.value.code == "rate_limited" and limited.value.retryable
    assert limited.value.pause_seconds == 7 and not limited.value.usage_limit
    assert "slow down" not in str(limited.value)

    adapter, client = make_adapter(
        "deepinfra", ReplayTransport("glm_midstream_error"), quiet(), model=GLM, api_key=KEY
    )
    seen: list[Any] = []
    with pytest.raises(ProviderError) as broken:
        async for event in adapter.stream(make_request()):
            seen.append(event)
    await client.aclose()
    assert seen == [TextDelta("hi")]
    assert broken.value.code == "provider_unavailable" and "overloaded" not in str(broken.value)


async def test_glm_cancel_closes_the_connection() -> None:
    transport = ReplayTransport("glm_text")
    adapter, client = make_adapter("deepinfra", transport, quiet(), model=GLM, api_key=KEY)
    stream = adapter.stream(make_request())
    assert isinstance(await anext(stream), TextDelta)
    await stream.aclose()
    await client.aclose()
    assert transport.streams[0].closed


# ------------------------------------------------------------ chat_only ports


def _json_transport(payload: dict[str, Any], captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("provider", "payload", "path", "key_header"),
    [
        (
            "anthropic",
            {
                "content": [{"type": "text", "text": "hi there"}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
            "/v1/messages",
            "x-api-key",
        ),
        (
            "gemini",
            {
                "candidates": [{"content": {"parts": [{"text": "hi there"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
            },
            "/v1beta/models/test-model:generateContent",
            "x-goog-api-key",
        ),
        (
            "ollama",
            {"message": {"content": "hi there"}, "prompt_eval_count": 3, "eval_count": 2},
            "/api/chat",
            "authorization",
        ),
    ],
)
async def test_chat_only_adapters_answer_in_one_delta(
    provider: str, payload: dict[str, Any], path: str, key_header: str
) -> None:
    captured: list[httpx.Request] = []
    adapter, client = make_adapter(
        provider, _json_transport(payload, captured), quiet(), api_key=KEY
    )
    events = await collect(
        adapter, make_request(ModelMessage("system", "Be brief."), ModelMessage("user", "hello"))
    )
    await client.aclose()
    assert events == [TextDelta("hi there"), ModelUsage(3, 2), ModelDone("stop")]
    assert captured[0].url.path == path
    assert key_header in captured[0].headers
    body = json.loads(captured[0].content)
    assert "Be brief." in json.dumps(body)


async def test_chat_only_refuses_tools_and_never_echoes_a_reflected_key() -> None:
    adapter, client = make_adapter(
        "anthropic", _json_transport({"content": []}, []), quiet(), api_key=KEY
    )
    with pytest.raises(ProviderError) as refused:
        await collect(adapter, make_request(tools=TOOLS))
    assert refused.value.code == "invalid_request"
    await client.aclose()

    reflect = {"content": [{"type": "text", "text": f"your key is {KEY}"}]}
    adapter, client = make_adapter("anthropic", _json_transport(reflect, []), quiet(), api_key=KEY)
    events = await collect(adapter, make_request())
    await client.aclose()
    assert KEY not in repr(events)
