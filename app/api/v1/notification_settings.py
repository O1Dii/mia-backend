"""``/v1/notifications/settings`` (contracts/mvp/v1/openapi.yaml, notifications.schema.json): which
channels the user receives Mia's notifications on, and for which kinds of events (M77).

Owner-only; Android (bearer) and web (``mia_session`` cookie) share one principal.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.jobs import IdempotencyKey, json_response, request_id
from app.modules.auth.principal import RequestAuthenticator
from app.modules.notifications.preferences import NotificationPreferences, PreferencesError

ERRORS: dict[str, tuple[int, str]] = {
    "notification_channel_unknown": (422, "The notification channel is unknown."),
    "notification_event_type_unknown": (422, "The notification event type is unknown."),
}


def _error(request: Request, code: str) -> JSONResponse:
    status, message = ERRORS[code]
    body = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id(request),
            "retryable": False,
        }
    }
    return json_response(request, body, status)


class UpdateNotificationSettingsBody(BaseModel):
    """``UpdateNotificationSettingsRequest``: a merge, absent names keep their value."""

    model_config = ConfigDict(extra="forbid")
    channels: dict[str, bool] | None = Field(default=None, max_length=32)
    matrix: dict[str, dict[str, bool]] | None = Field(default=None, max_length=8)


def build_notification_settings_router(
    *, preferences: NotificationPreferences, authenticator: RequestAuthenticator
) -> APIRouter:
    router = APIRouter(tags=["Notifications"])

    async def user_of(request: Request) -> uuid.UUID:
        principal = await authenticator.authenticate(request)
        return uuid.UUID(principal.user_id)

    @router.get("/v1/notifications/settings")
    async def get_settings(request: Request) -> JSONResponse:
        return json_response(request, await preferences.snapshot(await user_of(request)))

    @router.patch("/v1/notifications/settings")
    async def update_settings(
        request: Request,
        body: Annotated[UpdateNotificationSettingsBody, Body()],
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        user = await user_of(request)
        try:
            snapshot = await preferences.update(user, channels=body.channels, matrix=body.matrix)
        except PreferencesError as error:
            return _error(request, error.code)
        return json_response(request, snapshot)

    return router
