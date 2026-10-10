"""MW2 wiring of the builtin plugins (M14) into the tool router (M13) and the catalog (M10).

- :class:`RouterServerFactory` adapts M14's ``(manifest, plugin_dir)`` factory to the
  router signature, where ``plugin_dir`` may be ``None``.
- :class:`BuiltinPluginCatalog` resolves builtin installs for ``POST /v1/installations``.
  Its ``tools_digest`` is :func:`compute_tools_digest` over the same ``tools/list`` the
  router sees, so an install made from the catalog is never blocked as a changed tool.
- :class:`GatewayHostLlm` gives plugins (``builtin.news``) the user's model through M11.

M15 replaces the catalog with the store; until then only builtin packages are installable.

- :class:`AlwaysOnInstallations` makes "Интернет" (search/news/read) available to every user
  without an installation (M73, ADR-0016): the router sees a virtual, always active
  installation whose digest is the current ``tools/list`` of the package.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer

from app.core.ports.model_gateway import (
    ModelDone,
    ModelGateway,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    TextDelta,
)
from app.core.runtime import Runtime
from app.modules.plugins.builtin import (
    BuiltinManifestSource,
    CallIdentity,
    HostLlmError,
    HostServices,
    InprocessFactory,
    LlmReply,
    SqlPluginStateStore,
    UnsupportedServerError,
    make_inprocess_factory,
)
from app.modules.plugins.builtin.web import PublicWebClient
from app.modules.plugins.registry import InstallationReader, InstallationRecord
from app.modules.tools.digest import ServerTool, compute_tools_digest
from app.modules.workspace.catalog import CatalogPlugin

logger = logging.getLogger(__name__)

HOST_LLM_MAX_OUTPUT_TOKENS = 1200
ALWAYS_ON_PLUGINS: tuple[str, ...] = ("builtin.internet",)
"""Builtin plugins every user has without installing them (M73)."""
SYSTEM_INSTALLATION_PREFIX = "system:"
SYSTEM_WORKSPACE_ID = "system"


class RouterServerFactory:
    """Router-side ``InProcessServerFactory`` over M14's factory."""

    def __init__(self, factory: InprocessFactory) -> None:
        self._factory = factory

    def __call__(self, manifest: Mapping[str, Any], plugin_dir: Path | None) -> MCPServer:
        if plugin_dir is None:
            raise UnsupportedServerError("in-process plugins need their package directory")
        return self._factory(manifest, plugin_dir)


async def list_server_tools(server: MCPServer) -> list[ServerTool]:
    """``tools/list`` of an in-process server, in the router's ``ServerTool`` form."""
    tools: list[ServerTool] = []
    async with Client(server, cache=None) as client:
        cursor: str | None = None
        while True:
            page = await client.list_tools(cursor=cursor)
            for raw in page.tools:
                schema = getattr(raw, "input_schema", None)
                if not isinstance(schema, dict):
                    continue
                tools.append(
                    ServerTool(
                        name=raw.name,
                        description=str(raw.description or ""),
                        input_schema=schema,
                    )
                )
            cursor = page.next_cursor
            if not cursor:
                return tools


class BuiltinPluginCatalog:
    """``PluginCatalog`` of the builtin packages, digests computed lazily and cached."""

    def __init__(self, source: BuiltinManifestSource, factory: RouterServerFactory) -> None:
        self._source = source
        self._factory = factory
        self._resolved: dict[tuple[str, str], CatalogPlugin] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, plugin_id: str, version: str) -> CatalogPlugin | None:
        key = (plugin_id, version)
        cached = self._resolved.get(key)
        if cached is not None:
            return cached
        plugin = self._source.get(plugin_id, version)
        if plugin is None:
            return None
        async with self._lock:
            cached = self._resolved.get(key)
            if cached is not None:
                return cached
            server = self._factory(plugin.manifest, plugin.directory)
            digest = compute_tools_digest(await list_server_tools(server))
            resolved = CatalogPlugin(
                plugin_id=plugin.plugin_id,
                version=plugin.version,
                tools_digest=digest,
                manifest=plugin.manifest,
            )
            self._resolved[key] = resolved
            return resolved


class AlwaysOnInstallations:
    """``InstallationReader`` that adds the always-on builtin plugins to the user's installs.

    The virtual installation is first in the list (so the plugin tool budget never trims
    it), status ``active``, id ``system:<plugin_id>``, with the digest of the package in this
    image. A user's own healthy installation of the same plugin is kept; a stale one (older
    digest, consent needed) is replaced, so search never disappears.
    """

    def __init__(
        self,
        inner: InstallationReader,
        source: BuiltinManifestSource,
        catalog: BuiltinPluginCatalog,
        plugin_ids: Sequence[str] = ALWAYS_ON_PLUGINS,
    ) -> None:
        self._inner = inner
        self._source = source
        self._catalog = catalog
        self._plugin_ids = tuple(plugin_ids)

    async def installations(
        self, *, user_id: str, workspace_id: str | None
    ) -> Sequence[InstallationRecord]:
        records = list(await self._inner.installations(user_id=user_id, workspace_id=workspace_id))
        workspace = workspace_id or (records[0].workspace_id if records else SYSTEM_WORKSPACE_ID)
        system: list[InstallationRecord] = []
        for plugin_id in self._plugin_ids:
            record = await self._system_record(plugin_id, workspace)
            if record is not None:
                system.append(record)
        # A healthy installation of the user (active, current digest) stays as it is; any
        # other row of the same plugin (stale digest, needs consent) yields to the virtual one.
        own = {record.plugin_id: record for record in records}
        extra: list[InstallationRecord] = []
        replaced: set[str] = set()
        for record in system:
            mine = own.get(record.plugin_id)
            if (
                mine is not None
                and mine.status == "active"
                and mine.tools_digest == record.tools_digest
            ):
                continue
            extra.append(record)
            replaced.add(record.plugin_id)
        return [*extra, *(record for record in records if record.plugin_id not in replaced)]

    async def _system_record(self, plugin_id: str, workspace_id: str) -> InstallationRecord | None:
        package = self._source.get(plugin_id)
        if package is None:
            return None
        try:
            resolved = await self._catalog.resolve(plugin_id, package.version)
        except Exception:
            logger.exception("always_on_plugin_unavailable", extra={"plugin_id": plugin_id})
            return None
        if resolved is None:
            return None
        return InstallationRecord(
            id=f"{SYSTEM_INSTALLATION_PREFIX}{plugin_id}",
            workspace_id=workspace_id,
            plugin_id=plugin_id,
            version=resolved.version,
            status="active",
            grants=(),
            tools_digest=resolved.tools_digest,
            manifest=resolved.manifest,
        )


class GatewayHostLlm:
    """``HostLlm`` over the user's model (profile ``job``, plain text, no tools)."""

    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    async def generate(self, prompt: str, *, identity: CallIdentity, plugin_id: str) -> LlmReply:
        try:
            info = await self._gateway.describe(user_id=identity.user_id)
            parts: list[str] = []
            async for event in self._gateway.stream(
                ModelRequest(
                    user_id=identity.user_id,
                    profile="job",
                    messages=(ModelMessage(role="user", content=prompt),),
                    structured_response=False,
                    max_output_tokens=HOST_LLM_MAX_OUTPUT_TOKENS,
                )
            ):
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                elif isinstance(event, ModelDone):
                    break
        except ModelGatewayError as error:
            raise HostLlmError("model unavailable") from error
        text = "".join(parts).strip()
        if not text:
            raise HostLlmError("empty model reply")
        return LlmReply(text=text, model=info.ref)


@dataclass(frozen=True, slots=True)
class BuiltinPlugins:
    source: BuiltinManifestSource
    server_factory: RouterServerFactory
    catalog: BuiltinPluginCatalog

    def always_on(self) -> Callable[[InstallationReader], InstallationReader]:
        """Wrapper for ``build_tool_stack(wrap_installations=...)`` (M73)."""

        def wrap(inner: InstallationReader) -> InstallationReader:
            return AlwaysOnInstallations(inner, self.source, self.catalog)

        return wrap


def build_builtin_plugins(runtime: Runtime, *, model_gateway: ModelGateway) -> BuiltinPlugins:
    services = HostServices(
        state=SqlPluginStateStore(runtime.database.session_factory),
        web=PublicWebClient(),
        llm=GatewayHostLlm(model_gateway),
    )
    source = BuiltinManifestSource()
    factory = RouterServerFactory(make_inprocess_factory(services))
    return BuiltinPlugins(source, factory, BuiltinPluginCatalog(source, factory))
