"""Adapters ported from ``web_poc`` and labelled ``chat_only`` (no tool guarantees).

Anthropic, Gemini and Ollama use their native text APIs with one bounded request per
step; the answer is emitted as a single ``TextDelta``. Tools are not supported: a
request that carries tools is rejected so the run engine never assumes an action ran.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import httpx

from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelRequest,
    ModelUsage,
    TextDelta,
)
from app.modules.models.adapters.base import (
    AdapterSettings,
    AdapterTarget,
    open_response,
    secret_value,
)
from app.modules.models.errors import ProviderError

MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_MAX_TOKENS = 2048


class ChatOnlyAdapter:
    """One-shot text adapter for ``anthropic``, ``gemini`` and ``ollama``."""

    def __init__(
        self, client: httpx.AsyncClient, target: AdapterTarget, settings: AdapterSettings
    ) -> None:
        self._client = client
        self._target = target
        self._settings = settings

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        if request.tools:
            raise ProviderError(
                "invalid_request", "This model connection is chat-only and cannot use tools."
            )
        if any(m.role == "tool" or m.tool_calls for m in request.messages):
            raise ProviderError("invalid_request", "Chat-only models cannot replay tool turns.")
        kind = self._target.spec.adapter
        path, body = _build(kind, self._target.model, request)
        key = secret_value(self._target.api_key)

        async def headers() -> dict[str, str]:
            return _headers(kind, key)

        async with open_response(
            self._client, self._target.base_url + path, body, headers, None, self._settings
        ) as response:
            raw = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ProviderError("provider_unavailable", retryable=True)
            except httpx.TimeoutException:
                raise ProviderError("provider_timeout", retryable=True) from None
            except httpx.HTTPError:
                raise ProviderError("provider_unavailable", retryable=True) from None
        text, tokens_in, tokens_out = _parse(kind, bytes(raw))
        if not text.strip():
            raise ProviderError(
                "provider_unavailable", "The model returned an empty answer.", retryable=True
            )
        if key:
            text = text.replace(key, "[hidden]")  # defense in depth against reflected credentials
        yield TextDelta(text)
        yield ModelUsage(input_tokens=tokens_in, output_tokens=tokens_out)
        yield ModelDone("stop")


def _build(kind: str, model: str, request: ModelRequest) -> tuple[str, dict[str, object]]:
    system = "\n\n".join(m.content for m in request.messages if m.role == "system" and m.content)
    turns = [m for m in request.messages if m.role != "system"]
    limit = request.max_output_tokens or DEFAULT_MAX_TOKENS
    if kind == "anthropic":
        body: dict[str, object] = {
            "model": model,
            "max_tokens": limit,
            "messages": [{"role": m.role, "content": m.content} for m in turns],
        }
        if system:
            body["system"] = system
        return "/messages", body
    if kind == "gemini":
        body = {
            "contents": [
                {
                    "role": "model" if m.role == "assistant" else "user",
                    "parts": [{"text": m.content}],
                }
                for m in turns
            ],
            "generationConfig": {"maxOutputTokens": limit},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return f"/models/{quote(model, safe='')}:generateContent", body
    messages = [{"role": m.role, "content": m.content} for m in request.messages]
    return "/api/chat", {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": limit},
    }


def _headers(kind: str, key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if kind == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif kind == "gemini":
        headers["x-goog-api-key"] = key
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _parse(kind: str, raw: bytes) -> tuple[str, int, int]:
    try:
        data: Any = json.loads(raw)
        if kind == "anthropic":
            text = "\n".join(p["text"] for p in data["content"] if p.get("type") == "text")
            usage = data.get("usage") or {}
            return text, _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
        if kind == "gemini":
            parts = data["candidates"][0]["content"]["parts"]
            text = "\n".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
            meta = data.get("usageMetadata") or {}
            return text, _int(meta.get("promptTokenCount")), _int(meta.get("candidatesTokenCount"))
        text = data["message"]["content"]
        return str(text), _int(data.get("prompt_eval_count")), _int(data.get("eval_count"))
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError(
            "provider_unavailable", "The provider returned an unsupported response format."
        ) from None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
