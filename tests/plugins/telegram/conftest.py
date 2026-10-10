"""M41 fixtures: migrated Postgres, a fake phone and bot API, the real Telegram plugin on the
real router with memory tools, and the plugin host."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.crypto import MasterKeyring
from app.core.ports.device_tool_channel import (
    DeviceToolCall,
    DeviceToolDeclaration,
    DeviceToolResult,
)
from app.modules.memory import MemoryCipher, MemoryService, MemoryTools, SqlRunPrivacy
from app.modules.memory.domain import PersonInput
from app.modules.models.vault import KeyVault
from app.modules.notifications.telegram import TelegramStack, build_telegram
from app.modules.notifications.telegram_api import InlineButton, SendResult
from app.modules.plugins.builtin import BuiltinManifestSource, HostServices
from app.modules.plugins.builtin.factory import build_inprocess_server
from app.modules.plugins.registry import (
    InstallationRecord,
    LoadedManifest,
    PluginRegistry,
    StaticInstallationReader,
    StaticManifestSource,
)
from app.modules.runs import NewRun, RunService
from app.modules.tools.approvals import InMemoryApprovalService
from app.modules.tools.audit import InMemoryToolAudit
from app.modules.tools.core_tools import CoreToolRegistry
from app.modules.tools.mcp_client import McpConnector
from app.modules.tools.router import McpToolRouter, RouterConfig
from app.plugins.builtin.telegram.host import bind_host
from app.plugins.builtin.telegram.runtime import MiaTelegramHost
from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.runs.conftest import _migrated_factory, create_users, requires_postgres
from tests.tools.helpers import digest_of

__all__ = ["requires_postgres"]

KEYRING = MasterKeyring({"test-1": bytes(range(32))}, "test-1")
PLUGIN_ID = "builtin.telegram"
BOT_TOKEN = "123456:TEST-TOKEN-NOT-REAL"
WEBHOOK_SECRET = "test-webhook-secret_0123456789"

CONTACTS = DeviceToolDeclaration(
    name="device.contacts.search",
    description="Search contacts",
    input_schema={"type": "object"},
    risk="read",
    voice=True,
    requires_unlocked=False,
    timeout_ms=3000,
)
HANDOFF = DeviceToolDeclaration(
    name="telegram.handoff",
    description="Open Telegram with a draft",
    input_schema={"type": "object"},
    risk="write_reversible",
    voice=True,
    requires_unlocked=True,
    timeout_ms=3000,
)


@dataclass
class FakePhone:
    """A phone declaring the M23 tools; ``handoff`` answers from ``handoff_result``."""

    contacts: list[str] = field(default_factory=list)
    declared: list[DeviceToolDeclaration] = field(default_factory=lambda: [CONTACTS, HANDOFF])
    handoffs: list[DeviceToolCall] = field(default_factory=list)
    searches: list[DeviceToolCall] = field(default_factory=list)
    handoff_result: DeviceToolResult | None = None

    async def register(
        self, *, user_id: str, device_id: str, tools: Sequence[DeviceToolDeclaration]
    ) -> None:
        return None

    async def available_tools(
        self, *, user_id: str, device_id: str
    ) -> Sequence[DeviceToolDeclaration]:
        return list(self.declared)

    async def invoke(self, *, user_id: str, call: DeviceToolCall) -> DeviceToolResult:
        if call.tool == "device.contacts.search":
            self.searches.append(call)
            query = str(call.arguments["query"]).casefold()
            found = [{"name": n, "phone_count": 1} for n in self.contacts if query in n.casefold()]
            return DeviceToolResult(
                call_id=call.call_id,
                status="succeeded",
                content=f"Found {len(found)} contacts",
                structured_content={"matches": found},
            )
        assert call.tool == "telegram.handoff", call.tool
        self.handoffs.append(call)
        if self.handoff_result is not None:
            return DeviceToolResult(
                call_id=call.call_id,
                status=self.handoff_result.status,
                content=self.handoff_result.content,
                structured_content=self.handoff_result.structured_content,
                error_code=self.handoff_result.error_code,
                error_message=self.handoff_result.error_message,
            )
        return DeviceToolResult(
            call_id=call.call_id,
            status="succeeded",
            content="Telegram opened with the draft; the user must press send",
            structured_content={"opened": True, "direct_chat": True, "sent": False},
        )

    async def deliver_result(
        self, *, user_id: str, device_id: str, result: DeviceToolResult
    ) -> bool:
        return False


@dataclass
class FakeBotApi:
    """The Bot API without a network: records every ``sendMessage``."""

    sent: list[tuple[int, str]] = field(default_factory=list)
    buttons: list[tuple[int, str, list[InlineButton]]] = field(default_factory=list)
    """Messages that carried inline buttons: ``(chat_id, text, buttons)``."""
    answered: list[str] = field(default_factory=list)
    edited: list[tuple[int, int, str]] = field(default_factory=list)
    outcomes: list[SendResult] = field(default_factory=list)

    async def send_message(
        self, chat_id: int, text: str, buttons: Sequence[InlineButton] | None = None
    ) -> SendResult:
        self.sent.append((chat_id, text))
        if buttons:
            self.buttons.append((chat_id, text, list(buttons)))
        return self.outcomes.pop(0) if self.outcomes else SendResult("sent")

    async def answer_callback(self, callback_id: str) -> None:
        self.answered.append(callback_id)

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        self.edited.append((chat_id, message_id, text))


@dataclass
class TelegramWorld:
    factory: async_sessionmaker[AsyncSession]
    users: list[uuid.UUID]
    service: MemoryService
    tools: MemoryTools
    phone: FakePhone
    bot: FakeBotApi
    stack: TelegramStack
    router: McpToolRouter
    approvals: InMemoryApprovalService
    installation: InstallationRecord
    device_id: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def user(self) -> uuid.UUID:
        return self.users[0]

    async def add_person(
        self, name: str, *, relation: str | None = None, username: str | None = None
    ) -> str:
        identifiers: dict[str, str | int] = {"telegram_username": username} if username else {}
        person, _ = await self.service.create_person(
            user_id=str(self.user),
            data=PersonInput(display_name=name, relation=relation, identifiers=identifiers),
        )
        return str(person.id)

    def new_run(self, text: str = "Напиши Яне", *, device: bool = True) -> NewRun:
        return NewRun(
            user_id=self.user,
            profile="chat",
            input_text=text,
            device_id=self.device_id if device else None,
        )


def telegram_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "telegram_bot_token": BOT_TOKEN,
        "telegram_bot_username": "mia_test_bot",
        "telegram_webhook_secret": WEBHOOK_SECRET,
    }
    values.update(overrides)
    return Settings(**values)


class _Database:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = factory


class _Runtime:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.database = _Database(factory)


@pytest.fixture
async def tg(tmp_path: Path) -> AsyncIterator[TelegramWorld]:
    factory = await _migrated_factory()
    users = await create_users(factory, 2)
    vault = KeyVault(KEYRING)
    service = MemoryService(factory, MemoryCipher(vault))
    phone, bot = FakePhone(), FakeBotApi()
    memory_tools = MemoryTools(service, SqlRunPrivacy(factory), channel=phone)
    core = CoreToolRegistry(memory_tools.core_tools())
    plugin = BuiltinManifestSource().get(PLUGIN_ID)
    assert plugin is not None
    manifest = dict(plugin.manifest)
    services = HostServices()

    def factory_for(raw: Any, plugin_dir: Path | None) -> MCPServer:
        return build_inprocess_server(raw, plugin_dir or plugin.directory, services=services)

    installation = InstallationRecord(
        id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        plugin_id=PLUGIN_ID,
        version=plugin.version,
        status="active",
        grants=("tools.write_external", "tools.write_reversible"),
        tools_digest=await digest_of(factory_for(manifest, plugin.directory)),
        manifest=manifest,
    )
    approvals = InMemoryApprovalService()
    router = McpToolRouter(
        registry=PluginRegistry(
            StaticInstallationReader({str(users[0]): [installation]}),
            StaticManifestSource([LoadedManifest(manifest, plugin.directory)]),
        ),
        connector=McpConnector(server_factory=factory_for),
        audit=InMemoryToolAudit(),
        approvals=approvals,
        device_channel=phone,
        core_tools=core,
        config=RouterConfig(call_timeout_s=5, list_cache_s=0, approval_ttl_s=2),
    )
    stack = build_telegram(telegram_settings(), _Runtime(factory), api=bot)  # type: ignore[arg-type]
    bind_host(
        MiaTelegramHost(
            memory=service,
            contacts=memory_tools.contacts,
            channel=phone,
            session_factory=factory,
            telegram=stack,
        )
    )
    yield TelegramWorld(
        factory, users, service, memory_tools, phone, bot, stack, router, approvals, installation
    )
    bind_host(None)
    await factory.kw["bind"].dispose()


async def approve_when_asked(
    service: RunService, world: TelegramWorld, run_id: uuid.UUID, decision: str = "approve"
) -> None:
    async for event in service.events(user_id=world.user, run_id=run_id):
        if event is not None and event.type == "approval.requested":
            approval = event.to_wire()["payload"]["approval"]
            await world.approvals.decide(
                user_id=str(world.user),
                approval_id=approval["id"],
                decision=decision,  # type: ignore[arg-type]
                args_hash=approval["args_hash"],
                channel="web",
            )
            return
