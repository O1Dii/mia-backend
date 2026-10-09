"""Adapter plumbing: resolved targets, the HTTP open/retry helper and shared mappers."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from pydantic import SecretStr

from app.core.ports.model_gateway import ModelEvent, ModelRequest
from app.modules.models.catalog import ProviderSpec
from app.modules.models.errors import ProviderError, from_http_error

MAX_ERROR_BODY = 4096


class TokenSource(Protocol):
    """A refreshable bearer source (``chatgpt-plan``)."""

    async def access_token(self) -> str: ...

    async def invalidate(self) -> None:
        """Drop the cached access token so the next call refreshes (after a 401)."""
        ...


class ModelAdapter(Protocol):
    def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent, None]:
        """One model step. Raises ``ProviderError``; a stream that raised is not resumed."""
        ...


@dataclass(frozen=True, slots=True)
class AdapterSettings:
    stream_deadline_seconds: float = 120.0
    pause_seconds: int = 3600
    max_retries: int = 2
    backoff_seconds: float = 0.5
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)


@dataclass(frozen=True, slots=True)
class AdapterTarget:
    """A resolved connection ready for an adapter; secrets are excluded from ``repr``."""

    spec: ProviderSpec
    model: str
    base_url: str
    api_key: SecretStr | None = field(default=None, repr=False)
    tokens: TokenSource | None = field(default=None, repr=False)


HeadersFactory = Callable[[], Awaitable[dict[str, str]]]


async def _send_with_retry(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, object],
    headers: HeadersFactory,
    tokens: TokenSource | None,
    settings: AdapterSettings,
) -> httpx.Response:
    attempt = 0
    reauthed = False
    while True:
        try:
            request = client.build_request("POST", url, headers=await headers(), json=body)
            response = await client.send(request, stream=True)
        except httpx.ConnectError:
            if attempt < settings.max_retries:
                await settings.sleep(settings.backoff_seconds * 2**attempt)
                attempt += 1
                continue
            raise ProviderError("provider_unavailable", retryable=True) from None
        except httpx.TimeoutException:
            raise ProviderError("provider_timeout", retryable=True) from None
        except httpx.HTTPError:
            raise ProviderError("provider_unavailable", retryable=True) from None
        if response.status_code < 400:
            return response
        try:
            raw = await response.aread()
        except httpx.HTTPError:
            raw = b""
        finally:
            await response.aclose()
        text = raw[:MAX_ERROR_BODY].decode("utf-8", errors="replace")
        if response.status_code == 401 and tokens is not None and not reauthed:
            reauthed = True
            await tokens.invalidate()
            continue
        if response.status_code in {502, 503} and attempt < settings.max_retries:
            await settings.sleep(settings.backoff_seconds * 2**attempt)
            attempt += 1
            continue
        raise from_http_error(
            response.status_code,
            text,
            retry_after=response.headers.get("retry-after"),
            pause_seconds=settings.pause_seconds,
        )


@asynccontextmanager
async def open_response(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, object],
    headers: HeadersFactory,
    tokens: TokenSource | None,
    settings: AdapterSettings,
) -> AsyncIterator[httpx.Response]:
    """POST ``body`` and yield a streaming 2xx response; always closes the connection.

    Retries connection errors and 502/503 with bounded backoff, refreshes once on 401
    when a token source exists, and maps every failure to a content-free ``ProviderError``.
    """
    response = await _send_with_retry(client, url, body, headers, tokens, settings)
    try:
        yield response
    finally:
        await response.aclose()


async def aiter_lines_safe(response: httpx.Response) -> AsyncGenerator[str, None]:
    """Iterate response lines, mapping transport failures to ``ProviderError``."""
    try:
        async for line in response.aiter_lines():
            yield line
    except httpx.TimeoutException:
        raise ProviderError("provider_timeout", retryable=True) from None
    except httpx.HTTPError:
        raise ProviderError("provider_unavailable", retryable=True) from None


def parse_arguments(raw: str) -> dict[str, object]:
    """Tool arguments must be a JSON object; anything else is a retryable model error."""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise ProviderError(
            "invalid_request", "The model produced malformed tool arguments.", retryable=True
        )
    return value


def secret_value(secret: SecretStr | None) -> str:
    return secret.get_secret_value() if secret is not None else ""
