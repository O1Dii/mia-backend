"""Wire Telegram (M41): the bot (webhook, link, ``telegram`` channel) and the plugin host.

In ``app/composition.py`` after ``install_jobs`` and ``install_memory``::

    jobs = install_jobs(...)
    install_telegram_plugin(app, settings, runtime, jobs=jobs, memory=memory, tools=tools)
"""

from __future__ import annotations

from fastapi import FastAPI

from app.core.config import Settings
from app.core.runtime import Runtime
from app.modules.jobs.composition import JobsStack
from app.modules.memory.composition import MemoryStack
from app.modules.notifications.telegram import TelegramStack, install_telegram
from app.modules.notifications.telegram_api import TelegramBotApi
from app.modules.tools.composition import ToolStack
from app.plugins.builtin.telegram.host import bind_host
from app.plugins.builtin.telegram.runtime import MiaTelegramHost


def install_telegram_plugin(
    app: FastAPI,
    settings: Settings,
    runtime: Runtime,
    *,
    jobs: JobsStack,
    memory: MemoryStack,
    tools: ToolStack,
    api: TelegramBotApi | None = None,
) -> TelegramStack:
    stack = install_telegram(
        app,
        settings,
        runtime,
        channels=jobs.notifications.channels,
        api=api,
        preferences=jobs.notifications.preferences,
    )
    bind_host(
        MiaTelegramHost(
            memory=memory.service,
            contacts=memory.tools.contacts,
            channel=tools.device_channel,
            session_factory=runtime.database.session_factory,
            telegram=stack,
        )
    )
    return stack
