"""Wire the Memory Ledger: service (``MemoryProvider``), core tools, API, retention.

Usage in ``app/composition.py`` (after ``install_models`` and ``install_tools``)::

    memory = install_memory(app, runtime, vault=models_stack.vault, tools=tools)
    runs = install_runs(..., memory=memory.service)
    memory.tools.bind_publisher(runs.publish)

With ``tools`` the stack also binds the router's ref-name resolver (approval text names
``person:<id>``/``contact:<n>`` for the run's owner, M41-R2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from fastapi import FastAPI

from app.api.v1.memory import build_memory_router
from app.core.runtime import Runtime
from app.modules.auth.composition import request_authenticator
from app.modules.memory.cipher import MemoryCipher
from app.modules.memory.errors import MemoryLedgerError, memory_error_handler
from app.modules.memory.names import MemoryRefNames
from app.modules.memory.retention import RetentionService, RetentionSweeper
from app.modules.memory.service import MemoryService
from app.modules.memory.tools import ContactsCache, MemoryTools, SqlRunPrivacy
from app.modules.models.vault import KeyVault
from app.modules.tools.composition import ToolStack


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    retention_sweep_s: float = 900.0
    """Interval of the history-retention sweep; 0 disables it."""
    incognito_run_ttl: timedelta = timedelta(hours=1)
    contacts_ttl_s: float = 900.0


@dataclass(frozen=True, slots=True)
class MemoryStack:
    service: MemoryService
    tools: MemoryTools
    retention: RetentionService
    sweeper: RetentionSweeper


def build_memory_stack(
    runtime: Runtime,
    *,
    vault: KeyVault,
    tools: ToolStack | None = None,
    config: MemoryConfig | None = None,
) -> MemoryStack:
    config = config or MemoryConfig()
    session_factory = runtime.database.session_factory
    service = MemoryService(session_factory, MemoryCipher(vault))
    memory_tools = MemoryTools(
        service,
        SqlRunPrivacy(session_factory),
        channel=tools.device_channel if tools is not None else None,
        contacts=ContactsCache(ttl_s=config.contacts_ttl_s),
    )
    if tools is not None:
        for tool in memory_tools.core_tools():
            tools.core_tools.register(tool)
        # Late binding (M41-R2): the router names people refs on the owner's approval card.
        tools.router.bind_ref_names(MemoryRefNames(service, memory_tools.contacts))
    retention = RetentionService(session_factory, incognito_ttl=config.incognito_run_ttl)
    return MemoryStack(
        service, memory_tools, retention, RetentionSweeper(retention, config.retention_sweep_s)
    )


def install_memory(
    app: FastAPI,
    runtime: Runtime,
    *,
    vault: KeyVault,
    tools: ToolStack | None = None,
    config: MemoryConfig | None = None,
) -> MemoryStack:
    """Mount ``/v1/memory*``, register core tools; exposed as ``app.state.memory``."""
    stack = build_memory_stack(runtime, vault=vault, tools=tools, config=config)
    app.add_exception_handler(MemoryLedgerError, memory_error_handler)
    app.include_router(
        build_memory_router(service=stack.service, authenticator=request_authenticator(app))
    )
    app.state.memory = stack

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with original(application):
            stack.sweeper.start()
            try:
                yield
            finally:
                await stack.sweeper.stop()

    app.router.lifespan_context = lifespan
    return stack
