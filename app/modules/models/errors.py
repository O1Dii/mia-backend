"""Gateway error mapping: upstream failures become content-free ``ModelGatewayError``s.

Messages are fixed strings. Upstream bodies, headers and credentials never reach
an exception message, a log record or an API response; only a validated
``provider_code`` token (e.g. ``rate_limit_exceeded``) is kept for diagnostics.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from app.core.ports.model_gateway import ModelGatewayError, ModelGatewayErrorCode

USAGE_LIMIT_CODE: Final = "subscription_sharing_usage_limit_exceeded"
_CODE_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

_MESSAGES: Final[dict[str, str]] = {
    "model_not_configured": "The model connection is missing, rejected or unavailable to you.",
    "invalid_request": "The model provider rejected the request.",
    "provider_unavailable": "The model provider is temporarily unavailable.",
    "provider_timeout": "The model provider did not answer in time.",
    "rate_limited": "The model provider rate limit or usage limit was reached.",
    "context_too_long": "The conversation is too long for the model.",
}


class ProviderError(ModelGatewayError):
    """A ``ModelGatewayError`` carrying content-free diagnostics for the gateway."""

    def __init__(
        self,
        code: ModelGatewayErrorCode,
        message: str | None = None,
        *,
        retryable: bool = False,
        provider_code: str | None = None,
        status: int | None = None,
        pause_seconds: int | None = None,
        needs_reauth: bool = False,
        usage_limit: bool = False,
    ) -> None:
        super().__init__(code, message or _MESSAGES[code], retryable=retryable)
        self.provider_code = (
            provider_code if provider_code and _CODE_TOKEN.match(provider_code) else None
        )
        self.status = status
        self.pause_seconds = pause_seconds
        self.needs_reauth = needs_reauth
        self.usage_limit = usage_limit

    @property
    def switchable(self) -> bool:
        """True when the connection should be taken out of rotation and a fallback tried."""
        return self.usage_limit or self.needs_reauth or self.code == "model_not_configured"


def parse_error_body(body: str) -> tuple[str | None, str | None]:
    """Return ``(code, type)`` from an upstream error body; never the message."""
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    err = payload.get("error")
    if isinstance(err, dict):
        code, etype = err.get("code"), err.get("type")
        return (str(code) if code else None, str(etype) if etype else None)
    if isinstance(err, str):
        return err, None
    return None, None


def retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(float(value))
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 86400 else None


def from_http_error(
    status: int, body: str, *, retry_after: str | None = None, pause_seconds: int
) -> ProviderError:
    """Map a pre-stream HTTP failure (status + body) to a ``ProviderError``."""
    code, etype = parse_error_body(body)
    token = code or etype
    if token == USAGE_LIMIT_CODE:
        return ProviderError(
            "rate_limited",
            "The ChatGPT plan usage limit was reached; the connection is paused.",
            provider_code=token,
            status=status,
            pause_seconds=retry_after_seconds(retry_after) or pause_seconds,
            usage_limit=True,
        )
    if token in {"context_length_exceeded", "string_above_max_length"}:
        return ProviderError("context_too_long", provider_code=token, status=status)
    if status == 401:
        return ProviderError(
            "model_not_configured", provider_code=token, status=status, needs_reauth=True
        )
    if status in {402, 403}:
        return ProviderError("model_not_configured", provider_code=token, status=status)
    if status == 404:
        return ProviderError("model_not_configured", provider_code=token, status=status)
    if status in {408, 504}:
        return ProviderError("provider_timeout", retryable=True, provider_code=token, status=status)
    if status == 429:
        return ProviderError(
            "rate_limited",
            retryable=True,
            provider_code=token,
            status=status,
            pause_seconds=retry_after_seconds(retry_after),
        )
    if status >= 500:
        return ProviderError(
            "provider_unavailable", retryable=True, provider_code=token, status=status
        )
    return ProviderError("invalid_request", provider_code=token, status=status)


def from_stream_failure(code: str | None, *, pause_seconds: int = 3600) -> ProviderError:
    """Map an in-stream failure event (``response.failed``/``error``) to a ``ProviderError``."""
    token = code or "unknown_error"
    if token == USAGE_LIMIT_CODE:
        return from_http_error(
            429, json.dumps({"error": {"code": token}}), pause_seconds=pause_seconds
        )
    if token in {"context_length_exceeded"}:
        return ProviderError("context_too_long", provider_code=token)
    if token in {"rate_limit_exceeded", "insufficient_quota"}:
        return ProviderError("rate_limited", retryable=True, provider_code=token)
    if token in {
        "invalid_prompt",
        "invalid_request_error",
        "unsupported_capability",
    } or token.endswith("_unsupported_capability"):
        return ProviderError("invalid_request", provider_code=token)
    return ProviderError("provider_unavailable", retryable=True, provider_code=token)
