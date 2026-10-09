"""MW2 wiring of the builtin plugins (M14) into the tool router (M13) and the catalog (M10).

- :class:`RouterServerFactory` adapts M14's ``(manifest, plugin_dir)`` factory to the
  router signature, where ``plugin_dir`` may be ``None``.
- :class:`BuiltinPluginCatalog` resolves builtin installs for ``POST /v1/installations``.
  Its ``tools_digest`` is :func:`compute_tools_digest` over the same ``tools/list`` the
  router sees, so an install made from the catalog is never blocked as a changed tool.
- :class:`GatewayHostLlm` gives plugins (``builtin.news``) the user's model through M11.

M15 replaces the catalog with the store; until then only builtin packages are installable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
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
from app.modules.tools.digest import ServerTool, compute_tools_digest
from app.modules.workspace.catalog import CatalogPlugin

HOST_LLM_MAX_OUTPUT_TOKENS = 1200


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


def build_builtin_plugins(runtime: Runtime, *, model_gateway: ModelGateway) -> BuiltinPlugins:
    services = HostServices(
        state=SqlPluginStateStore(runtime.database.session_factory),
        web=PublicWebClient(),
        llm=GatewayHostLlm(model_gateway),
    )
    source = BuiltinManifestSource()
    factory = RouterServerFactory(make_inprocess_factory(services))
    return BuiltinPlugins(source, factory, BuiltinPluginCatalog(source, factory))
