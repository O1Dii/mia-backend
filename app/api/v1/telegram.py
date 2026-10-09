"""``/v1/telegram/*`` (contracts/mvp/v1/openapi.yaml): bot link status, deep link and webhook.

``POST|GET|DELETE /v1/telegram/link`` are first-party (bearer or ``mia_session`` cookie); the
``Idempotency-Key`` of POST is required by the contract but a retry issues a new token (a replayed
response would store the secret), older tokens simply expire.
``POST /webhooks/telegram`` is called by Telegram and authenticated only by the secret
header (constant-time compare); a valid call always answers 200 so Telegram does not retry
an update that failed on our side.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api.v1.jobs import IdempotencyKey, empty_response, json_response, request_id
from app.modules.auth.principal import RequestAuthenticator
from app.modules.notifications.telegram import TelegramStack

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_UPDATE_BYTES = 64 * 1024

ERRORS: dict[str, tuple[int, str]] = {
    "telegram_not_configured": (503, "The Telegram bot is not configured on this server."),
    "telegram_webhook_forbidden": (403, "The webhook secret is missing or wrong."),
}


def _error(request: Request, code: str) -> JSONResponse:
    status, message = ERRORS[code]
    return json_response(
        request,
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id(request),
                "retryable": False,
            }
        },
        status,
    )


def secret_matches(expected: str | None, supplied: str | None) -> bool:
    """Constant-time comparison; a missing secret on either side never matches."""
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def build_telegram_router(
    *, stack: TelegramStack, authenticator: RequestAuthenticator
) -> APIRouter:
    router = APIRouter(tags=["Telegram"])

    async def user_of(request: Request) -> uuid.UUID:
        principal = await authenticator.authenticate(request)
        return uuid.UUID(principal.user_id)

    @router.get("/v1/telegram/link")
    async def link_status(request: Request) -> JSONResponse:
        user = await user_of(request)
        chat = await stack.links.chat_for(user)
        body: dict[str, Any] = {
            "available": stack.configured and stack.webhook_secret is not None,
            "linked": chat is not None,
            "bot_username": stack.bot_username,
            "linked_at": chat.created_at.isoformat().replace("+00:00", "Z") if chat else None,
        }
        return json_response(request, body)

    @router.post("/v1/telegram/link", status_code=201)
    async def create_link(request: Request, idempotency_key: IdempotencyKey) -> JSONResponse:
        user = await user_of(request)
        if not stack.configured or stack.webhook_secret is None:
            return _error(request, "telegram_not_configured")
        offer = await stack.links.create_offer(user)
        url = stack.deep_link(offer.token)
        return json_response(
            request,
            {
                "deep_link": url,
                "bot_username": stack.bot_username,
                "expires_at": offer.expires_at.isoformat().replace("+00:00", "Z"),
            },
            201,
        )

    @router.delete("/v1/telegram/link", status_code=204)
    async def delete_link(request: Request, idempotency_key: IdempotencyKey) -> Response:
        user = await user_of(request)
        await stack.links.unlink_user(user)
        return empty_response(request)

    @router.post("/webhooks/telegram")
    async def webhook(request: Request) -> JSONResponse:
        if stack.bot is None or stack.webhook_secret is None:
            return _error(request, "telegram_not_configured")
        if not secret_matches(stack.webhook_secret, request.headers.get(SECRET_HEADER)):
            return _error(request, "telegram_webhook_forbidden")
        try:
            raw = await request.body()
            update = None if len(raw) > MAX_UPDATE_BYTES else await request.json()
        except ValueError:
            update = None
        if isinstance(update, dict):
            try:
                await stack.bot.handle_update(update)
            except Exception:
                logger.exception("telegram_update_failed")
        return json_response(request, {"ok": True})

    return router
