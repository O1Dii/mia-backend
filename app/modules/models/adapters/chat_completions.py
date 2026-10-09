"""OpenAI-compatible ``/chat/completions`` streaming adapter (DeepInfra GLM, Groq, Mistral, local).

DeepInfra ``zai-org/GLM-5.3-Flash`` is the verified fallback (M01): ``reasoning_content``
streamed by the model must be returned on later turns. It is kept in
``ModelToolCall.provider_state`` (never shown to clients) and replayed verbatim.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, Literal

import httpx

from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ModelUsage,
    TextDelta,
    ToolCallRequested,
)
from app.modules.models.adapters.base import (
    AdapterSettings,
    AdapterTarget,
    aiter_lines_safe,
    open_response,
    parse_arguments,
    secret_value,
)
from app.modules.models.errors import ProviderError, from_stream_failure
from app.modules.models.sse import iter_sse

STATE_KEY = "reasoning_content"
FinishReason = Literal["stop", "tool_calls", "length", "content_filter"]


def build_messages(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role in {"system", "user"}:
            out.append({"role": message.role, "content": message.content})
        elif message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            reasoning = _reasoning(message)
            if reasoning:
                entry[STATE_KEY] = reasoning
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            out.append(entry)
        else:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": message.content,
                }
            )
    return out


def _reasoning(message: ModelMessage) -> str:
    for state in [message.provider_state, *(c.provider_state for c in message.tool_calls)]:
        value = state.get(STATE_KEY) if state else None
        if isinstance(value, str) and value:
            return value
    return ""


class ChatCompletionsAdapter:
    def __init__(
        self, client: httpx.AsyncClient, target: AdapterTarget, settings: AdapterSettings
    ) -> None:
        self._client = client
        self._target = target
        self._settings = settings

    def build_body(self, request: ModelRequest) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self._target.model,
            "messages": build_messages(list(request.messages)),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.input_schema),
                    },
                }
                for tool in request.tools
            ]
        if request.max_output_tokens is not None:
            body["max_tokens"] = request.max_output_tokens
        return body

    async def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = secret_value(self._target.api_key)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        started = self._settings.clock()
        slots: dict[int, dict[str, str]] = {}
        reasoning = ""
        finish: str | None = None
        usage: dict[str, Any] | None = None
        saw_done = False
        async with open_response(
            self._client,
            f"{self._target.base_url}/chat/completions",
            self.build_body(request),
            self._headers,
            None,
            self._settings,
        ) as response:
            async with aclosing(iter_sse(aiter_lines_safe(response))) as events:
                async for _name, raw in events:
                    if self._settings.clock() - started > self._settings.stream_deadline_seconds:
                        raise ProviderError("provider_timeout", retryable=True)
                    if raw.strip() == "[DONE]":
                        saw_done = True
                        break
                    try:
                        chunk = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if isinstance(chunk.get("error"), dict):
                        code = chunk["error"].get("code") or chunk["error"].get("type")
                        raise from_stream_failure(
                            str(code) if code else None, pause_seconds=self._settings.pause_seconds
                        )
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        thought = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(thought, str):
                            reasoning += thought
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            yield TextDelta(text)
                        for call in delta.get("tool_calls") or []:
                            slot = slots.setdefault(
                                int(call.get("index", 0)),
                                {"call_id": "", "name": "", "arguments": ""},
                            )
                            fn = call.get("function") or {}
                            slot["call_id"] = call.get("id") or slot["call_id"]
                            slot["name"] = fn.get("name") or slot["name"]
                            slot["arguments"] += fn.get("arguments") or ""
                        if choice.get("finish_reason"):
                            finish = str(choice["finish_reason"])
        if not saw_done and finish is None:
            raise ProviderError(
                "provider_unavailable", "The model stream ended early.", retryable=True
            )
        calls = [slots[i] for i in sorted(slots)]
        for index, call in enumerate(calls):
            state = {STATE_KEY: reasoning} if reasoning and index == 0 else None
            yield ToolCallRequested(
                ModelToolCall(
                    call_id=call["call_id"],
                    name=call["name"],
                    arguments=parse_arguments(call["arguments"]),
                    provider_state=state,
                )
            )
        if usage is not None:
            yield self._usage(usage)
        yield ModelDone(_finish_reason(finish, bool(calls)))

    def _usage(self, usage: dict[str, Any]) -> ModelUsage:
        tokens_in = _int(usage.get("prompt_tokens"))
        tokens_out = _int(usage.get("completion_tokens"))
        cost = usage.get("estimated_cost")
        cost_usd: float | None = float(cost) if isinstance(cost, int | float) else None
        price = self._target.spec.price_per_million
        if cost_usd is None and price is not None:
            cost_usd = (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
        return ModelUsage(input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=cost_usd)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _finish_reason(raw: str | None, has_calls: bool) -> FinishReason:
    if has_calls or raw in {"tool_calls", "function_call"}:
        return "tool_calls"
    if raw == "length":
        return "length"
    if raw == "content_filter":
        return "content_filter"
    return "stop"
