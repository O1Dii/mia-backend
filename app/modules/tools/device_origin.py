"""Run origin = the requester of device tools (ADR-0016 §3, M70).

``POST /v1/runs`` calls :meth:`RunOriginResolver.prepare` before it starts a run and
:meth:`RunOriginResolver.commit` after. ``prepare`` replaces the client-supplied
``device_id`` by the verified one:

- bearer (the app): the device of the caller's refresh-token family. A ``device_id`` the
  app sent is replaced by that device (another device cannot be claimed); without one the
  run has no device, as before;
- cookie of the app's WebView (web session bound to a device by the M24 hand-off): that
  device — the WebView behaves as the phone;
- cookie of a browser: never a device (a claimed one is dropped). Device tools then need
  consent on the phone.

The origin (kind, label, devices) is kept for the run so the router can name the requester
and bind persistent grants to "this browser" or "this device".
"""

from __future__ import annotations

import dataclasses
import logging
import re
import uuid
from typing import Protocol

from fastapi import Request

from app.modules.auth.principal import AuthenticationFailure, RequestPrincipal
from app.modules.runs.domain import NewRun, RunRecord
from app.modules.tools.device_access import DeviceAccess
from app.modules.tools.device_store import RunOrigin

logger = logging.getLogger(__name__)

_BROWSERS = (
    ("YaBrowser", "Яндекс Браузер"),
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Mac OS X", "macOS"),
    ("Windows", "Windows"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)
_SAFE = re.compile(r"[^\w .()/-]")


class PrincipalAuthenticator(Protocol):
    async def authenticate(self, request: Request) -> RequestPrincipal: ...


def browser_label(user_agent: str | None) -> str:
    """«браузер Chrome на macOS» from a User-Agent; never echoes the raw header."""
    agent = user_agent or ""
    browser = next((name for needle, name in _BROWSERS if needle in agent), None)
    system = next((name for needle, name in _SYSTEMS if needle in agent), None)
    label = "браузер"
    if browser:
        label += f" {browser}"
    if system:
        label += f" на {system}"
    return _SAFE.sub("", label)[:128]


def device_label(name: str | None, *, webview: bool) -> str:
    base = f"Mia на телефоне «{_SAFE.sub('', name)[:60]}»" if name else "Mia на телефоне"
    return base + (" (страница в приложении)" if webview else "")


class RunOriginResolver:
    def __init__(self, access: DeviceAccess, authenticator: PrincipalAuthenticator) -> None:
        self._access = access
        self._authenticator = authenticator

    async def _origin(self, request: Request, new: NewRun) -> tuple[RunOrigin | None, str | None]:
        """Return the origin and the verified device id the run may carry."""
        try:
            principal = await self._authenticator.authenticate(request)
        except AuthenticationFailure:
            return None, None
        store = self._access.store
        user_id, run_id = principal.user_id, str(new.id)
        if principal.method == "bearer":
            device = await store.session_device(user_id=user_id, family_id=principal.session_id)
            if device is None:
                return RunOrigin(run_id, user_id, "unknown", "приложение Mia"), None
            claimed = str(new.device_id) if new.device_id is not None else None
            if claimed is not None and claimed != device:
                logger.warning("run_device_claim_replaced")
            name = await store.device_name(user_id=user_id, device_id=device)
            origin = RunOrigin(
                run_id, user_id, "device_app", device_label(name, webview=False), device_id=device
            )
            return origin, (device if claimed is not None else None)
        device = await store.web_session_device(
            user_id=user_id, web_session_id=principal.session_id
        )
        if device is not None:
            name = await store.device_name(user_id=user_id, device_id=device)
            origin = RunOrigin(
                run_id,
                user_id,
                "device_webview",
                device_label(name, webview=True),
                device_id=device,
                web_session_id=principal.session_id,
            )
            return origin, device
        label = browser_label(request.headers.get("user-agent"))
        return (
            RunOrigin(run_id, user_id, "web", label, web_session_id=principal.session_id),
            None,
        )

    async def prepare(self, request: Request, new: NewRun) -> NewRun:
        try:
            origin, device = await self._origin(request, new)
        except Exception:
            logger.warning("run_origin_resolve_failed")
            origin, device = None, None
        if origin is not None:
            # Known before the engine's first tool call, even if the database write lags.
            self._access.remember(origin)
        return dataclasses.replace(new, device_id=uuid.UUID(device) if device else None)

    async def commit(self, record: RunRecord, *, replayed: bool) -> None:
        if replayed:
            return
        origin = await self._access.origin(user_id=str(record.user_id), run_id=str(record.id))
        if origin is not None:
            await self._access.record_origin(origin)
