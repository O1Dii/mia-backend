"""Small text adapters. Credentials never leave the server except to their provider."""

import json
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

ProviderId = Literal[
    "openai", "anthropic", "gemini", "deepinfra", "groq", "mistral", "ollama", "local"
]

PROVIDERS: dict[str, dict[str, str | bool]] = {
    "openai": {"name": "OpenAI", "url": "https://api.openai.com/v1", "local": False},
    "anthropic": {"name": "Anthropic", "url": "https://api.anthropic.com/v1", "local": False},
    "gemini": {
        "name": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta",
        "local": False,
    },
    "deepinfra": {
        "name": "DeepInfra",
        "url": "https://api.deepinfra.com/v1/openai",
        "local": False,
    },
    "groq": {"name": "Groq", "url": "https://api.groq.com/openai/v1", "local": False},
    "mistral": {"name": "Mistral", "url": "https://api.mistral.ai/v1", "local": False},
    "ollama": {"name": "Ollama", "url": "http://127.0.0.1:11434", "local": True},
    "local": {"name": "LM Studio / local", "url": "http://127.0.0.1:1234/v1", "local": True},
}


@dataclass
class Connection:
    id: str
    provider: ProviderId
    label: str
    model: str
    base_url: str
    key: SecretStr = field(repr=False)
    tool_mode: Literal["native", "json"] = "native"

    def public(self) -> dict[str, str | bool]:
        return {
            "id": self.id,
            "provider": self.provider,
            "label": self.label,
            "model": self.model,
            "base_url": self.base_url,
            "has_key": bool(self.key.get_secret_value()),
            "tool_mode": self.tool_mode,
        }


class ProviderFailure(Exception):
    """A content-free public failure: never includes an upstream response or key."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def resolve_url(provider: ProviderId, custom: str | None) -> str:
    default = str(PROVIDERS[provider]["url"])
    if not custom:
        return default
    if not PROVIDERS[provider]["local"]:
        raise ValueError("Облачный провайдер использует фиксированный адрес API.")
    parsed = urlsplit(custom)
    # Literal loopback only: no arbitrary destinations, DNS rebinding or redirects.
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise ValueError("Локальный адрес: http://127.0.0.1:порт или http://[::1]:порт/v1.")
    try:
        if not parsed.port or parsed.port < 1024:
            raise ValueError
    except ValueError:
        raise ValueError("Укажите локальный порт от 1024 до 65535.") from None
    return custom.rstrip("/")


async def generate(
    connection: Connection,
    messages: list[dict[str, str]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """One bounded, non-streaming text turn using the explicitly chosen model."""
    provider = connection.provider
    headers = {"Content-Type": "application/json"}
    key = connection.key.get_secret_value()
    payload: dict[str, Any]
    if provider == "openai":
        path = "/responses"
        headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": connection.model,
            "input": messages,
            "store": False,
            "max_output_tokens": 2048,
        }
    elif provider == "anthropic":
        path = "/messages"
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        payload = {"model": connection.model, "messages": messages, "max_tokens": 2048}
    elif provider == "gemini":
        path = f"/models/{quote(connection.model, safe='')}:generateContent"
        headers["x-goog-api-key"] = key
        payload = {
            "contents": [
                {
                    "role": "model" if m["role"] == "assistant" else "user",
                    "parts": [{"text": m["content"]}],
                }
                for m in messages
            ],
            "generationConfig": {"maxOutputTokens": 2048},
        }
    elif provider == "ollama":
        if key:
            headers["Authorization"] = f"Bearer {key}"
        path = "/api/chat"
        payload = {
            "model": connection.model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": 2048},
        }
    else:
        path = "/chat/completions"
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {"model": connection.model, "messages": messages, "max_tokens": 2048}

    try:
        data = await request_json(connection, path, payload, headers, transport=transport)
        if provider == "openai":
            result = "\n".join(
                part["text"]
                for item in data["output"]
                if item.get("type") == "message"
                for part in item["content"]
                if part.get("type") == "output_text"
            )
        elif provider == "anthropic":
            result = "\n".join(p["text"] for p in data["content"] if p["type"] == "text")
        elif provider == "gemini":
            result = "\n".join(
                p["text"]
                for p in data["candidates"][0]["content"]["parts"]
                if "text" in p and not p.get("thought")
            )
        elif provider == "ollama":
            result = data["message"]["content"]
        else:
            result = data["choices"][0]["message"]["content"]
        if not isinstance(result, str) or not result.strip():
            raise ProviderFailure("Модель вернула пустой текст или отказала в ответе.")
        # Defense in depth against an upstream reflecting the credential into its content.
        return result.replace(key, "[скрыто]") if key else result
    except httpx.TimeoutException:
        raise ProviderFailure("Модель не ответила за 60 секунд. Попробуйте снова.", 504) from None
    except httpx.HTTPError:
        raise ProviderFailure("Не удалось подключиться к API модели.") from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderFailure("Провайдер вернул неподдерживаемый формат ответа.") from None


async def request_json(
    connection: Connection,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Content-free errors; cap upstream response size and never follow redirects."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=5),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        ) as client:
            async with client.stream(
                "POST", connection.base_url + path, headers=headers, json=payload
            ) as response:
                if response.status_code in {401, 403}:
                    raise ProviderFailure("Провайдер отклонил ключ или доступ к модели.")
                if response.status_code == 429:
                    raise ProviderFailure("Лимит провайдера исчерпан. Попробуйте позже.", 429)
                if not 200 <= response.status_code < 300:
                    raise ProviderFailure(
                        "Проверьте модель и доступ к API. Если tools не поддерживаются, "
                        "выберите JSON-режим в подключении."
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise ProviderFailure("Ответ провайдера превысил допустимый размер.")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ProviderFailure("Провайдер вернул неподдерживаемый формат ответа.")
                return data
    except httpx.TimeoutException:
        raise ProviderFailure("Модель не ответила вовремя. Попробуйте снова.", 504) from None
    except httpx.HTTPError:
        raise ProviderFailure("Не удалось подключиться к API модели.") from None
    except ValueError:
        raise ProviderFailure("Провайдер вернул неподдерживаемый формат ответа.") from None
