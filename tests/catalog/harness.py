"""Production wiring of the store on Postgres with in-process remote servers."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
from app.core.config import Settings
from app.core.crypto import MasterKeyring, generate_master_key
from app.core.database import Database
from app.core.ports.model_gateway import ModelGateway
from app.core.redis import RedisClient
from app.core.runtime import Runtime
from app.main import create_app
from app.modules.auth.cache import RedisAuthCache
from app.modules.auth.composition import install_auth
from app.modules.auth.service import AuthService
from app.modules.auth.tokens import TokenManager
from app.modules.connections.oauth.client import OAuthClient
from app.modules.connections.oauth.service import ConnectionService
from app.modules.models.vault import KeyVault
from app.modules.plugins.builtin import BUILTIN_ROOT
from app.modules.plugins.catalog.package import PluginPackage, load_package
from app.modules.plugins.catalog.service import CatalogService, PublishResult
from app.modules.plugins.catalog.showcase import showcase_for
from app.modules.plugins.composition import BuiltinPlugins, build_builtin_plugins
from app.modules.plugins.install.composition import (
    CatalogStack,
    StoreStack,
    build_catalog_stack,
    install_store,
)
from app.modules.plugins.registry import PluginRegistry, SqlInstallationReader, SqlRiskStore
from app.modules.tools.approvals import SqlApprovalService
from app.modules.tools.audit import SqlToolAudit
from app.modules.tools.composition import ToolStack
from app.modules.tools.core_tools import CoreToolRegistry
from app.modules.tools.device import InMemoryDeviceToolChannel
from app.modules.tools.mcp_client import McpConnector
from app.modules.tools.router import McpToolRouter, RouterConfig
from app.modules.users import UserRepository
from app.modules.workspace.composition import install_workspace
from app.modules.workspace.service import WorkspaceService
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.auth.fakes import FakeRedis
from tests.catalog.remote_world import (
    AuthorizationServer,
    FakeClock,
    FakeResolver,
    RemoteWorld,
    running,
    transport_factory,
)
from tests.workspace.harness import (
    BASE_URL,
    CLIENT_ID,
    SECRET,
    TokenGoogleVerifier,
    android_login,
    database_url,
    key,
    migrate_to_head,
)

PUBLIC_BASE = "https://mia.test.example"
ADMIN_SUB = "store-admin"


class _Bearer:
    def __init__(self, token: str | None) -> None:
        self._token = token

    async def headers(self, **_: Any) -> dict[str, str] | None:
        return {"Authorization": f"Bearer {self._token}"} if self._token else None


@dataclass
class Store:
    app: FastAPI
    client: httpx.AsyncClient
    database: Database
    factory: async_sessionmaker[AsyncSession]
    clock: FakeClock
    auth_server: AuthorizationServer
    remote: RemoteWorld
    resolver: FakeResolver
    catalog: CatalogService
    stack: CatalogStack
    connections: ConnectionService
    store: StoreStack
    workspace: WorkspaceService
    tools: ToolStack
    router: McpToolRouter
    builtin: BuiltinPlugins
    oauth: OAuthClient
    admin_id: str
    admin: dict[str, str]

    async def login(self, subject: str) -> dict[str, str]:
        return await android_login(self.client, subject)

    async def user_id(self, headers: dict[str, str]) -> str:
        response = await self.client.get("/v1/workspace", headers=headers)
        assert response.status_code == 200, response.text
        return str(response.json()["owner_user_id"])

    async def publish_builtin(self, name: str) -> PublishResult:
        package = load_package(BUILTIN_ROOT / name)
        return await self.catalog.publish(
            package, actor_user_id=self.admin_id, store_meta=showcase_for(package.plugin_id)
        )

    async def publish(self, package: PluginPackage, *, token: str | None = None) -> PublishResult:
        return await self.catalog.publish(
            package, actor_user_id=self.admin_id, discover_token=token
        )

    async def get(self, headers: dict[str, str], path: str, **params: Any) -> httpx.Response:
        return await self.client.get(path, headers=headers, params=params or None)

    async def post(
        self, headers: dict[str, str], path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        return await self.client.post(path, headers={**headers, **key()}, json=body or {})

    async def delete(self, headers: dict[str, str], path: str) -> httpx.Response:
        return await self.client.delete(path, headers={**headers, **key()})


@asynccontextmanager
async def build_store(
    *,
    require_auth: bool = True,
    access_ttl: int = 3600,
    supports_registration: bool = True,
    camel_case_tool: bool = True,
    annotated: bool = False,
    public_base_url: str = PUBLIC_BASE,
) -> AsyncIterator[Store]:
    await asyncio.to_thread(migrate_to_head)
    database = Database(database_url())
    async with database.session_factory() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE users CASCADE"))
        await session.execute(text("TRUNCATE TABLE plugin_releases"))
        admin = await UserRepository(session).create(
            google_sub=ADMIN_SUB,
            email="admin@example.test",
            email_verified=True,
            locale="ru-BY",
            timezone="Europe/Minsk",
        )
    admin_id = str(admin.id)
    settings = Settings(
        app_env="test",
        tools_list_cache_seconds=0,
        public_base_url=public_base_url,
        store_admin_user_ids=admin_id,
        auth_nonce_rate_limit=1000,
        auth_exchange_rate_limit=1000,
        auth_authenticated_rate_limit=5000,
    )
    runtime = cast(Runtime, SimpleNamespace(database=database))
    cache = RedisAuthCache(FakeRedis())
    auth_service = AuthService(
        database.session_factory,
        cache,
        TokenGoogleVerifier(),
        TokenManager(SECRET),
        google_client_id=CLIENT_ID,
    )
    app = create_app(settings, install_modules=False)
    redis = RedisClient(settings.redis_url)
    install_auth(app, settings, database=database, redis=redis, service=auth_service, cache=cache)

    clock = FakeClock()
    auth_server = AuthorizationServer(
        clock=clock, access_ttl=access_ttl, supports_registration=supports_registration
    )
    remote = RemoteWorld(
        auth_server, require_auth=require_auth, camel_case_tool=camel_case_tool, annotated=annotated
    )
    resolver = FakeResolver()
    oauth = OAuthClient(resolver=resolver, http_transport=transport_factory(remote), clock=clock)
    builtin = build_builtin_plugins(runtime, model_gateway=cast(ModelGateway, object()))
    stack = build_catalog_stack(settings, runtime, builtin)
    stack.service._connector_factory = lambda token: McpConnector(  # noqa: SLF001
        server_factory=builtin.server_factory,
        credentials=_Bearer(token),
        resolver=resolver,
        http_transport=transport_factory(remote),
    )
    workspace = install_workspace(app, database=database, catalog=stack.catalog)
    vault = KeyVault(MasterKeyring.from_config(f"k1:{generate_master_key()}"))
    connections = ConnectionService(
        database.session_factory,
        vault,
        oauth,
        workspace,
        public_base_url=public_base_url,
        clock=lambda: datetime.fromtimestamp(clock.now, UTC),
    )
    workspace.uninstall_hooks.append(connections.revoke_installation)
    connector = McpConnector(
        server_factory=builtin.server_factory,
        credentials=connections,
        resolver=resolver,
        http_transport=transport_factory(remote),
    )
    registry = PluginRegistry(
        SqlInstallationReader(database.session_factory), stack.manifest_source
    )
    channel = InMemoryDeviceToolChannel()
    core_tools = CoreToolRegistry()
    approvals = SqlApprovalService(database.session_factory)
    router = McpToolRouter(
        registry=registry,
        connector=connector,
        audit=SqlToolAudit(database.session_factory),
        approvals=approvals,
        device_channel=channel,
        core_tools=core_tools,
        config=RouterConfig(call_timeout_s=5, list_cache_s=0),
        risk_store=SqlRiskStore(database.session_factory),
    )
    tools = ToolStack(router, approvals, channel, core_tools, registry, connector)
    installed = install_store(
        app,
        settings,
        runtime,
        catalog=stack,
        connections=connections,
        tools=tools,
        workspace=workspace,
    )
    async with (
        running(remote),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client,
    ):
        admin_headers = await android_login(client, ADMIN_SUB)
        yield Store(
            app=app,
            client=client,
            database=database,
            factory=database.session_factory,
            clock=clock,
            auth_server=auth_server,
            remote=remote,
            resolver=resolver,
            catalog=stack.service,
            stack=stack,
            connections=connections,
            store=installed,
            workspace=workspace,
            tools=tools,
            router=router,
            builtin=builtin,
            oauth=oauth,
            admin_id=admin_id,
            admin=admin_headers,
        )
    await database.close()
    await redis.close()


def new_id() -> str:
    return str(uuid.uuid4())


__all__ = ["Store", "build_store", "key", "new_id"]
