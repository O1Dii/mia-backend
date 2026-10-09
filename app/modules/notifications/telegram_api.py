"""Telegram Bot API client (``sendMessage``, ``setWebhook``) for the Mia bot (M41).

The token is part of the request URL, so errors are logged by class only, never with the URL
or the request text. Tests inject an ``httpx.MockTransport`` (no network).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

logger = logging.getLogger(__name__)

SendOutcome = Literal["sent", "blocked", "retry", "rejected"]
MAX_MESSAGE_CHARS = 4096


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: SendOutcome
    error_code: str | None = None
    retry_after_s: float | None = None


class TelegramBotApi(Protocol):
    async def send_message(self, chat_id: int, text: str) -> SendResult: ...


class HttpTelegramBotApi:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._transport = transport

    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token}/{method}"

    async def _post(self, method: str, payload: dict[str, object]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as http:
            return await http.post(self._url(method), json=payload)

    async def send_message(self, chat_id: int, text: str) -> SendResult:
        body = text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 1] + "…"
        try:
            response = await self._post(
                "sendMessage",
                {"chat_id": chat_id, "text": body, "disable_web_page_preview": True},
            )
        except httpx.HTTPError as error:
            logger.warning("telegram_send_failed", extra={"error": type(error).__name__})
            return SendResult("retry", "network_error")
        return classify_response(response)

    async def set_webhook(self, url: str, secret: str) -> bool:
        """Register ``url`` with the secret header; allowed updates: messages only."""
        try:
            response = await self._post(
                "setWebhook",
                {"url": url, "secret_token": secret, "allowed_updates": ["message"]},
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and bool(_json(response).get("ok"))

    async def delete_webhook(self) -> bool:
        try:
            response = await self._post("deleteWebhook", {})
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and bool(_json(response).get("ok"))


def _json(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def classify_response(response: httpx.Response) -> SendResult:
    status = response.status_code
    if status == 200 and _json(response).get("ok") is True:
        return SendResult("sent")
    if status == 429:
        parameters = _json(response).get("parameters")
        wait = parameters.get("retry_after") if isinstance(parameters, dict) else None
        return SendResult(
            "retry", "rate_limited", float(wait) if isinstance(wait, int | float) else None
        )
    if status >= 500:
        return SendResult("retry", "telegram_unavailable")
    if status == 403:
        return SendResult("blocked", "bot_blocked")
    description = str(_json(response).get("description", "")).lower()
    if status == 400 and ("chat not found" in description or "deactivated" in description):
        return SendResult("blocked", "chat_not_found")
    if status == 401:
        return SendResult("rejected", "bot_token_rejected")
    return SendResult("rejected", f"telegram_http_{status}")
