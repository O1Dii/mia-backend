"""``PUT /v1/devices/push-token``: the device's FCM registration token for push delivery.

Only ``/v1/devices/push-token`` lives here; device tool declarations
(``/v1/devices/{device_id}/tools``) are M23's (``app/api/v1/device_tools.py``).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.jobs import IdempotencyKey, empty_response, error_response
from app.modules.auth.principal import RequestAuthenticator
from app.modules.notifications.channels import DeviceNotFoundError, PushTokenStore


class PushTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: uuid.UUID
    provider: Literal["fcm"]
    token: str = Field(min_length=1, max_length=4096)


def build_devices_router(
    *, tokens: PushTokenStore, authenticator: RequestAuthenticator
) -> APIRouter:
    router = APIRouter(tags=["Devices"])

    @router.put("/v1/devices/push-token", status_code=204)
    async def put_push_token(
        request: Request, body: Annotated[PushTokenBody, Body()], idempotency_key: IdempotencyKey
    ) -> Response:
        principal = await authenticator.authenticate(request)
        try:
            await tokens.put(
                user_id=uuid.UUID(principal.user_id),
                device_id=body.device_id,
                provider=body.provider,
                token=body.token,
            )
        except DeviceNotFoundError:
            return error_response(request, "device_not_found")
        return empty_response(request)

    return router
