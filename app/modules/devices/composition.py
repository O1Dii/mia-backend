"""Wire «Устройства» (M84): the directory, presence observation and ``/v1/devices`` routes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.device_directory import build_device_directory_router
from app.modules.auth.composition import request_authenticator
from app.modules.devices.presence import PresenceTracker
from app.modules.devices.service import DeviceDirectory, PluginLister
from app.modules.devices.store import SqlBrowserSource, SqlDirectoryStore, SqlPhoneSource
from app.modules.tools.device_access import DeviceAccess


def push_configured() -> bool:
    """FCM is set up on the server (the M16 ``FCM_*`` settings, read-only here)."""
    from app.modules.jobs.settings import JobsSettings  # noqa: PLC0415 (settings read once)

    settings = JobsSettings()
    return settings.fcm_project_id is not None and settings.fcm_credentials_file is not None


def build_directory(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    access: DeviceAccess | None,
    plugins: PluginLister | None,
    push: bool,
    clock: Callable[[], datetime] | None = None,
) -> DeviceDirectory:
    tracker = PresenceTracker(clock) if clock is not None else PresenceTracker()
    return DeviceDirectory(
        SqlDirectoryStore(session_factory),
        [SqlPhoneSource(session_factory, push_configured=push), SqlBrowserSource(session_factory)],
        tracker=tracker,
        access=access,
        plugins=plugins,
    )


def install_devices(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    access: DeviceAccess | None,
    plugins: PluginLister | None,
    push: bool | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DeviceDirectory:
    """Mount ``/v1/devices`` (list, card, rename, disconnect, heartbeat) after ``install_tools``.

    Every authenticated request becomes a contact of its device; the directory is also
    ``app.state.devices`` (the phone-only grant/log routes of M70 ask it whether a session
    belongs to the phone's owner).
    """
    directory = build_directory(
        session_factory,
        access=access,
        plugins=plugins,
        push=push_configured() if push is None else push,
        clock=clock,
    )
    authenticator = request_authenticator(app)
    authenticator.add_observer(directory.observe)
    app.include_router(
        build_device_directory_router(directory=directory, authenticator=authenticator)
    )
    app.state.devices = directory
    return directory
