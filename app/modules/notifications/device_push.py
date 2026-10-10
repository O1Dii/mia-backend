"""Pushes to one device for device access (ADR-0016, M70).

Two FCM **data-only** messages (high priority); the app builds what the user sees:

- ``device_consent``: «<клиент> просит: <возможность> …» with actions «Разрешить» /
  «Отклонить» (the app posts ``/v1/approvals/{id}/decision``; «Разрешить» means this
  request, the persistent choice is made in the app). TTL = time left to decide.
- ``device_tool_call``: wake-up for a call queued for this device; the app reads
  ``GET /v1/devices/{device_id}/tool-calls`` and posts the result as usual.

Payload keys are the contract ``device-access.schema.json#/$defs/DevicePushConsent`` and
``#/$defs/DevicePushToolCall`` (all values strings). Arguments, results and contact data
never travel through FCM.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.approval_service import Approval
from app.core.ports.device_tool_channel import DeviceToolCall
from app.modules.notifications.fcm import FcmClient, PushMessage
from app.modules.notifications.models import PushTokenRow

logger = logging.getLogger(__name__)

CONSENT_TITLE = "Mia просит разрешение"
CALL_TTL_SECONDS = 60


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def consent_message(approval: Approval, *, now: datetime) -> PushMessage:
    consent = approval.device_consent or {}
    requester = consent.get("requester")
    requester_label = requester.get("label") if isinstance(requester, dict) else None
    scopes = consent.get("scopes")
    data = {
        "type": "device_consent",
        "approval_id": approval.id,
        "run_id": approval.run_id,
        "tool": approval.tool,
        "capability": str(consent.get("capability") or ""),
        "capability_label": str(consent.get("capability_label") or ""),
        "requester_label": str(requester_label or ""),
        "model_sees": str(consent.get("model_sees") or ""),
        "summary": approval.summary,
        "args_hash": approval.args_hash,
        "expires_at": _iso(approval.expires_at),
        "scopes": ",".join(str(item) for item in scopes) if isinstance(scopes, list) else "run",
        "title": CONSENT_TITLE,
    }
    ttl = max(1, int((approval.expires_at - now).total_seconds()))
    return PushMessage(
        title=CONSENT_TITLE,
        body=approval.summary,
        data=data,
        collapse_key=approval.id,
        data_only=True,
        ttl_seconds=ttl,
    )


def call_message(call: DeviceToolCall) -> PushMessage:
    data = {
        "type": "device_tool_call",
        "run_id": call.run_id,
        "call_id": call.call_id,
        "tool": call.tool,
        "deadline_at": _iso(call.deadline_at),
    }
    return PushMessage(
        title="",
        body="",
        data=data,
        collapse_key=f"call:{call.call_id}"[:64],
        data_only=True,
        ttl_seconds=CALL_TTL_SECONDS,
    )


class DevicePush:
    """``DeviceNotifier`` over FCM; without FCM configured no device is push-reachable."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: FcmClient | None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._clock = clock

    async def _tokens(self, user_id: str, device_id: str) -> list[tuple[uuid.UUID, str]]:
        try:
            user, device = uuid.UUID(user_id), uuid.UUID(device_id)
        except ValueError:
            return []
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(PushTokenRow.id, PushTokenRow.token).where(
                        PushTokenRow.user_id == user,
                        PushTokenRow.device_id == device,
                        PushTokenRow.provider == "fcm",
                        PushTokenRow.invalidated_at.is_(None),
                    )
                )
            ).all()
        return [(row[0], row[1]) for row in rows]

    async def reachable(self, *, user_id: str, device_id: str) -> bool:
        return self._client is not None and bool(await self._tokens(user_id, device_id))

    async def consent_requested(self, *, user_id: str, device_id: str, approval: Approval) -> bool:
        return await self._send(user_id, device_id, consent_message(approval, now=self._clock()))

    async def call_waiting(self, *, user_id: str, device_id: str, call: DeviceToolCall) -> bool:
        return await self._send(user_id, device_id, call_message(call))

    async def _send(self, user_id: str, device_id: str, message: PushMessage) -> bool:
        client = self._client
        if client is None:
            return False
        sent = False
        for token_id, token in await self._tokens(user_id, device_id):
            result = await client.send(token, message)
            if result.outcome == "sent":
                sent = True
            elif result.outcome == "unregistered":
                await self._invalidate(token_id)
            else:
                logger.warning("device_push_failed", extra={"error_code": result.error_code})
        return sent

    async def _invalidate(self, token_id: uuid.UUID) -> None:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(PushTokenRow)
                .where(PushTokenRow.id == token_id)
                .values(invalidated_at=now, updated_at=now)
            )


def build_device_push(session_factory: async_sessionmaker[AsyncSession]) -> DevicePush:
    """Production notifier: FCM settings are the M16 ones (``FCM_*``, read-only here)."""
    from app.modules.jobs.settings import JobsSettings

    settings = JobsSettings()
    client: FcmClient | None = None
    if settings.fcm_project_id is not None and settings.fcm_credentials_file is not None:
        from app.modules.notifications.fcm import ServiceAccountTokenProvider

        client = FcmClient(
            project_id=settings.fcm_project_id,
            token_provider=ServiceAccountTokenProvider(
                settings.fcm_credentials_file, timeout_s=settings.fcm_timeout_seconds
            ),
            endpoint=settings.fcm_endpoint,
            timeout_s=settings.fcm_timeout_seconds,
        )
    return DevicePush(session_factory, client)
