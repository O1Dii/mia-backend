"""Memory fixtures: migrated Postgres, users, a ``MemoryService`` and a router with memory tools."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.crypto import MasterKeyring
from app.core.ports.device_tool_channel import (
    DeviceToolCall,
    DeviceToolDeclaration,
    DeviceToolResult,
)
from app.core.ports.types import JsonObject
from app.modules.memory import MemoryCipher, MemoryService, MemoryTools, SqlRunPrivacy
from app.modules.models.vault import KeyVault
from app.modules.plugins.registry import (
    PluginRegistry,
    StaticInstallationReader,
    StaticManifestSource,
)
from app.modules.tools.approvals import InMemoryApprovalService
from app.modules.tools.audit import InMemoryToolAudit
from app.modules.tools.core_tools import CoreToolRegistry
from app.modules.tools.mcp_client import McpConnector
from app.modules.tools.router import McpToolRouter, RouterConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.runs.conftest import _migrated_factory, create_users

KEYRING = MasterKeyring({"test-1": bytes(range(32))}, "test-1")

CONTACTS_DECLARATION = DeviceToolDeclaration(
    name="device.contacts.search",
    description="Найти контакт телефона по имени",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "maxLength": 100}},
        "required": ["query"],
        "additionalProperties": False,
    },
    risk="read",
    voice=True,
    requires_unlocked=False,
    timeout_ms=3000,
)


@dataclass
class FakeContactsChannel:
    """A phone that declares ``device.contacts.search`` and answers from a list."""

    contacts: list[dict[str, Any]] = field(default_factory=list)
    calls: list[DeviceToolCall] = field(default_factory=list)

    async def register(
        self, *, user_id: str, device_id: str, tools: Sequence[DeviceToolDeclaration]
    ) -> None:
        return None

    async def available_tools(
        self, *, user_id: str, device_id: str
    ) -> Sequence[DeviceToolDeclaration]:
        return [CONTACTS_DECLARATION]

    async def invoke(self, *, user_id: str, call: DeviceToolCall) -> DeviceToolResult:
        self.calls.append(call)
        query = str(call.arguments.get("query", "")).lower()
        found = [c for c in self.contacts if query[:3] in str(c["display_name"]).lower()]
        structured: JsonObject = {"contacts": found}
        lines = "; ".join(f"{c['display_name']} {c.get('phones')}" for c in found)
        return DeviceToolResult(
            call_id=call.call_id, status="succeeded", content=lines, structured_content=structured
        )

    async def deliver_result(
        self, *, user_id: str, device_id: str, result: DeviceToolResult
    ) -> bool:
        return False


@dataclass
class MemoryWorld:
    factory: async_sessionmaker[AsyncSession]
    users: list[uuid.UUID]
    vault: KeyVault
    service: MemoryService

    @property
    def user(self) -> uuid.UUID:
        return self.users[0]

    def router(
        self, channel: FakeContactsChannel | None = None
    ) -> tuple[McpToolRouter, MemoryTools]:
        tools = MemoryTools(self.service, SqlRunPrivacy(self.factory), channel=channel)
        core = CoreToolRegistry(tools.core_tools())
        router = McpToolRouter(
            registry=PluginRegistry(StaticInstallationReader({}), StaticManifestSource([])),
            connector=McpConnector(),
            audit=InMemoryToolAudit(),
            approvals=InMemoryApprovalService(),
            device_channel=channel,
            core_tools=core,
            config=RouterConfig(call_timeout_s=5, list_cache_s=0),
        )
        return router, tools


@pytest.fixture
async def mem() -> AsyncIterator[MemoryWorld]:
    factory = await _migrated_factory()
    users = await create_users(factory, 2)
    vault = KeyVault(KEYRING)
    yield MemoryWorld(factory, users, vault, MemoryService(factory, MemoryCipher(vault)))
    await factory.kw["bind"].dispose()


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]
