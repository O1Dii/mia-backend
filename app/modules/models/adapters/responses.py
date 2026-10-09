"""OpenAI Responses API adapter: ``chatgpt-plan`` (primary, ADR-0014) and API-key ``openai``.

Wire contract: ``spikes/m01/DECISION.md``. ``store`` is always false and ``stream`` true;
in plan mode the request carries only parameters the plan endpoint is known to accept
(no ``temperature``/``max_output_tokens``/``previous_response_id``/...).

Provider state (closes OI-W6-013): the function-call turn is replayed in the verified
*plain* mode (``function_call`` without ``id``, no reasoning items). When the model returns
reasoning items that carry ``encrypted_content`` they are preserved verbatim in
``ModelToolCall.provider_state`` and replayed before the calls on the next request.
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

STATE_KEY = "responses_reasoning_items"
FinishReason = Literal["stop", "tool_calls", "length", "content_filter"]


def build_input(messages: list[ModelMessage]) -> tuple[str | None, list[dict[str, Any]]]:
    """Map neutral messages to ``(instructions, input items)``."""
    system = [m.content for m in messages if m.role == "system" and m.content]
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "user":
            items.append({"role": "user", "content": message.content})
        elif message.role == "assistant":
            if message.content:
                items.append({"role": "assistant", "content": message.content})
            items.extend(_reasoning_items(message))
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    }
                )
        else:
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    "output": message.content,
                }
            )
    return ("\n\n".join(system) if system else None), items


def _reasoning_items(message: ModelMessage) -> list[dict[str, Any]]:
    states = [message.provider_state, *(c.provider_state for c in message.tool_calls)]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for state in states:
        raw = state.get(STATE_KEY) if state else None
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                marker = json.dumps(item, sort_keys=True)
                if marker not in seen:
                    seen.add(marker)
                    out.append(dict(item))
    return out


class ResponsesAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        target: AdapterTarget,
        settings: AdapterSettings,
        *,
        plan_mode: bool,
        include_encrypted_reasoning: bool = False,
    ) -> None:
        self._client = client
        self._target = target
        self._settings = settings
        self._plan_mode = plan_mode
        self._include_encrypted = include_encrypted_reasoning

    def build_body(self, request: ModelRequest) -> dict[str, object]:
        instructions, items = build_input(list(request.messages))
        body: dict[str, object] = {
            "model": self._target.model,
            "input": items,
            "store": False,
            "stream": True,
        }
        if instructions:
            body["instructions"] = instructions
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.input_schema),
                }
                for tool in request.tools
            ]
            body["tool_choice"] = "auto"
        if self._include_encrypted:
            body["include"] = ["reasoning.encrypted_content"]
        if request.max_output_tokens is not None and not self._plan_mode:
            body["max_output_tokens"] = request.max_output_tokens
        return body

    async def _headers(self) -> dict[str, str]:
        tokens = self._target.tokens
        bearer = (
            await tokens.access_token()
            if tokens is not None
            else secret_value(self._target.api_key)
        )
        return {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        started = self._settings.clock()
        calls: list[dict[str, str]] = []
        reasoning: list[dict[str, Any]] = []
        done = False
        async with open_response(
            self._client,
            f"{self._target.base_url}/responses",
            self.build_body(request),
            self._headers,
            self._target.tokens,
            self._settings,
        ) as response:
            async with aclosing(iter_sse(aiter_lines_safe(response))) as events:
                async for name, raw in events:
                    if self._settings.clock() - started > self._settings.stream_deadline_seconds:
                        raise ProviderError("provider_timeout", retryable=True)
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    etype = str(data.get("type") or name)
                    if etype == "response.output_text.delta":
                        delta = str(data.get("delta", ""))
                        if delta:
                            yield TextDelta(delta)
                    elif etype == "response.output_item.done":
                        item = data.get("item")
                        if isinstance(item, dict):
                            self._collect_item(item, calls, reasoning)
                    elif etype in {"response.completed", "response.incomplete"}:
                        for event in self._finish(data, etype, calls, reasoning):
                            yield event
                        done = True
                        break
                    elif etype in {"response.failed", "error"}:
                        raise self._failure(data)
        if not done:
            raise ProviderError(
                "provider_unavailable", "The model stream ended early.", retryable=True
            )

    @staticmethod
    def _collect_item(
        item: dict[str, Any], calls: list[dict[str, str]], reasoning: list[dict[str, Any]]
    ) -> None:
        kind = item.get("type")
        if kind == "function_call":
            calls.append(
                {
                    "call_id": str(item.get("call_id", "")),
                    "name": str(item.get("name", "")),
                    "arguments": str(item.get("arguments", "")),
                }
            )
        elif kind == "reasoning" and item.get("encrypted_content"):
            reasoning.append(item)

    def _finish(
        self,
        data: dict[str, Any],
        etype: str,
        calls: list[dict[str, str]],
        reasoning: list[dict[str, Any]],
    ) -> list[ModelEvent]:
        response = data.get("response")
        response = response if isinstance(response, dict) else {}
        events: list[ModelEvent] = []
        for index, call in enumerate(calls):
            state: dict[str, object] | None = (
                {STATE_KEY: reasoning} if reasoning and index == 0 else None
            )
            events.append(
                ToolCallRequested(
                    ModelToolCall(
                        call_id=call["call_id"],
                        name=call["name"],
                        arguments=parse_arguments(call["arguments"]),
                        provider_state=state,
                    )
                )
            )
        usage = response.get("usage")
        if isinstance(usage, dict):
            events.append(
                ModelUsage(
                    input_tokens=_int(usage.get("input_tokens")),
                    output_tokens=_int(usage.get("output_tokens")),
                )
            )
        events.append(ModelDone(_finish_reason(response, etype, bool(calls))))
        return events

    def _failure(self, data: dict[str, Any]) -> ProviderError:
        response = data.get("response")
        container = (response.get("error") if isinstance(response, dict) else None) or data.get(
            "error"
        )
        err = container if isinstance(container, dict) else data
        code = err.get("code") or err.get("type")
        return from_stream_failure(
            str(code) if code else None, pause_seconds=self._settings.pause_seconds
        )


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _finish_reason(response: dict[str, Any], etype: str, has_calls: bool) -> FinishReason:
    if has_calls:
        return "tool_calls"
    details = response.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if reason == "content_filter":
        return "content_filter"
    if etype == "response.incomplete" or reason == "max_output_tokens":
        return "length"
    return "stop"


__all__ = ["STATE_KEY", "ResponsesAdapter", "build_input"]
