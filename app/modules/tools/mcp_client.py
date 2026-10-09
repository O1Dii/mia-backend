"""MCP client side of the router: in-process and Streamable HTTP servers.

One short-lived SDK ``Client`` per operation (``tools/list`` or ``tools/call``):
the 2026-07-28 protocol is stateless per request, connect negotiates with
``server/discover`` and falls back to ``initialize`` for older servers.

- In-process servers (builtin, declarative) come from an injected
  :data:`InProcessServerFactory`; the router never imports plugin code.
- Remote servers are reached through :class:`GuardedTransport` (SSRF guard)
  with headers from an injected :class:`RemoteCredentials` (M15 connections).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, Implementation, RequestParamsMeta

from app.core.ports.types import JsonObject
from app.modules.plugins.registry import InstalledPlugin
from app.modules.plugins.registry.manifest import RemoteAuth
from app.modules.tools.digest import ServerTool
from app.modules.tools.netguard import GuardedTransport, HostBlockedError, HostPolicy, Resolver
from app.modules.tools.netguard import system_resolver as _system_resolver

logger = logging.getLogger(__name__)

InProcessServer = Server[Any] | MCPServer
InProcessServerFactory = Callable[[JsonObject, Path | None], InProcessServer]
"""``factory(manifest, plugin_dir) -> server`` for ``server.kind`` builtin/declarative.

Called once per plugin id + version; the returned SDK server is reused for
every user (per-user state is keyed by ``_meta["mia/installation_id"]``)."""

HttpTransportFactory = Callable[[], httpx2.AsyncBaseTransport]
CLIENT_INFO = Implementation(name="mia", version="1.0.0")
MAX_TOOLS = 128

Phase = Literal["connect", "call"]
FailureKind = Literal["blocked", "unavailable", "timeout", "error_response", "transport"]


class RemoteCredentials(Protocol):
    async def headers(
        self, *, user_id: str, installation_id: str, plugin_id: str, auth: RemoteAuth
    ) -> Mapping[str, str] | None:
        """Authorization headers for this user's connection, or None if not connected."""
        ...


class McpFailure(Exception):
    """A list/call did not produce a result.

    ``phase == "connect"``: nothing reached the tool (safe to report ``failed``).
    ``phase == "call"``: the request may have been delivered.
    """

    def __init__(self, phase: Phase, kind: FailureKind) -> None:
        super().__init__(f"{phase}:{kind}")
        self.phase = phase
        self.kind = kind


class ConnectionRequired(Exception):
    """A remote plugin needs an OAuth/Mia-token connection the user lacks."""


@dataclass(frozen=True, slots=True)
class McpResult:
    text: str
    structured: object | None
    is_error: bool


def _leaves(error: BaseException) -> Iterable[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            yield from _leaves(inner)
    else:
        yield error
        if error.__cause__ is not None:
            yield error.__cause__


def _classify(error: BaseException, phase: Phase) -> McpFailure:
    leaves = list(_leaves(error))
    if any(isinstance(leaf, HostBlockedError) for leaf in leaves):
        return McpFailure("connect", "blocked")
    if any(isinstance(leaf, TimeoutError | httpx2.TimeoutException) for leaf in leaves):
        if any(isinstance(leaf, httpx2.ConnectTimeout) for leaf in leaves):
            return McpFailure("connect", "unavailable")
        return McpFailure(phase, "timeout")
    if any(isinstance(leaf, httpx2.ConnectError) for leaf in leaves):
        return McpFailure("connect", "unavailable")
    if any(isinstance(leaf, MCPError) for leaf in leaves):
        return McpFailure(phase, "error_response" if phase == "call" else "unavailable")
    return McpFailure(phase, "transport" if phase == "call" else "unavailable")


def _logged(failure: McpFailure, plugin: InstalledPlugin, error: BaseException) -> McpFailure:
    """Log the failure class only: exception text may carry tool or server data."""
    logger.warning(
        "mcp_failure",
        extra={
            "plugin_id": plugin.plugin_id,
            "phase": failure.phase,
            "kind": failure.kind,
            "error_type": type(error).__name__,
        },
    )
    return failure


def result_text(result: CallToolResult) -> str:
    """Text of ``content`` blocks (model material); non-text blocks become markers."""
    parts: list[str] = []
    for block in result.content:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(str(getattr(block, "text", "")))
        elif kind == "resource_link":
            parts.append(f"[resource {getattr(block, 'uri', '')}]")
        elif kind == "resource":
            resource = getattr(block, "resource", None)
            text = getattr(resource, "text", None)
            parts.append(str(text) if isinstance(text, str) else "[embedded resource]")
        else:
            parts.append(f"[{kind or 'content'}]")
    return "\n".join(part for part in parts if part)


def _server_tool(raw: Any) -> ServerTool | None:
    name = getattr(raw, "name", None)
    schema = getattr(raw, "input_schema", None)
    if not isinstance(name, str) or not isinstance(schema, dict):
        return None
    meta = getattr(raw, "meta", None)
    return ServerTool(
        name=name,
        description=str(getattr(raw, "description", None) or ""),
        input_schema=schema,
        meta=meta if isinstance(meta, dict) else None,
    )


class McpConnector:
    def __init__(
        self,
        *,
        server_factory: InProcessServerFactory | None = None,
        credentials: RemoteCredentials | None = None,
        resolver: Resolver | None = None,
        http_transport: HttpTransportFactory | None = None,
        global_allowlist: Iterable[str] | None = None,
        list_timeout_s: float = 8.0,
    ) -> None:
        self._server_factory = server_factory
        self._credentials = credentials
        self._resolver: Resolver = resolver or _system_resolver
        self._http_transport = http_transport
        self._global_allowlist = (
            frozenset(global_allowlist) if global_allowlist is not None else None
        )
        self._list_timeout_s = list_timeout_s
        self._servers: dict[tuple[str, str], InProcessServer] = {}

    def _in_process(self, plugin: InstalledPlugin) -> InProcessServer:
        key = (plugin.manifest.plugin_id, plugin.manifest.version)
        server = self._servers.get(key)
        if server is None:
            if self._server_factory is None:
                raise McpFailure("connect", "unavailable")
            server = self._server_factory(plugin.manifest.raw, plugin.plugin_dir)
            self._servers[key] = server
        return server

    async def _remote_headers(self, plugin: InstalledPlugin, user_id: str) -> Mapping[str, str]:
        spec = plugin.manifest.server
        if spec.auth == "none":
            return {}
        if self._credentials is None:
            raise ConnectionRequired()
        headers = await self._credentials.headers(
            user_id=user_id,
            installation_id=plugin.installation.id,
            plugin_id=plugin.plugin_id,
            auth=spec.auth,
        )
        if not headers:
            raise ConnectionRequired()
        return headers

    @contextlib.asynccontextmanager
    async def _client(
        self, plugin: InstalledPlugin, user_id: str, timeout_s: float
    ) -> AsyncIterator[Client]:
        spec = plugin.manifest.server
        if spec.in_process:
            async with Client(self._in_process(plugin), cache=None, client_info=CLIENT_INFO) as c:
                yield c
            return
        assert spec.url is not None
        headers = await self._remote_headers(plugin, user_id)
        transport = GuardedTransport(
            HostPolicy(plugin.manifest.network, global_allowlist=self._global_allowlist),
            resolver=self._resolver,
            inner=self._http_transport() if self._http_transport else None,
        )
        http_client = httpx2.AsyncClient(
            transport=transport,
            headers=dict(headers),
            timeout=httpx2.Timeout(timeout_s),
            follow_redirects=False,
            trust_env=False,
        )
        async with http_client:
            client = Client(
                streamable_http_client(spec.url, http_client=http_client, terminate_on_close=False),
                cache=None,
                client_info=CLIENT_INFO,
            )
            async with client:
                yield client

    async def list_tools(self, plugin: InstalledPlugin, *, user_id: str) -> list[ServerTool]:
        """``tools/list`` (all pages, bounded). Raises ``McpFailure``/``ConnectionRequired``."""
        tools: list[ServerTool] = []
        try:
            async with asyncio.timeout(self._list_timeout_s):
                async with self._client(plugin, user_id, self._list_timeout_s) as client:
                    cursor: str | None = None
                    while len(tools) < MAX_TOOLS:
                        page = await client.list_tools(cursor=cursor)
                        for raw in page.tools:
                            tool = _server_tool(raw)
                            if tool is not None:
                                tools.append(tool)
                        cursor = page.next_cursor
                        if not cursor:
                            break
        except (ConnectionRequired, McpFailure):
            raise
        except Exception as error:
            raise _logged(_classify(error, "connect"), plugin, error) from error
        return tools[:MAX_TOOLS]

    async def call_tool(
        self,
        plugin: InstalledPlugin,
        *,
        user_id: str,
        name: str,
        arguments: JsonObject,
        meta: Mapping[str, Any],
        timeout_s: float,
    ) -> McpResult:
        """``tools/call`` once (never retried). Raises ``McpFailure``/``ConnectionRequired``."""
        phase: Phase = "connect"
        try:
            async with asyncio.timeout(timeout_s):
                async with self._client(plugin, user_id, timeout_s) as client:
                    phase = "call"
                    result = await client.call_tool(
                        name, dict(arguments), meta=cast(RequestParamsMeta, dict(meta))
                    )
        except (ConnectionRequired, McpFailure):
            raise
        except Exception as error:
            raise _logged(_classify(error, phase), plugin, error) from error
        return McpResult(
            text=result_text(result),
            structured=result.structured_content,
            is_error=bool(result.is_error),
        )

    async def read_resource(
        self, plugin: InstalledPlugin, *, user_id: str, uri: str, timeout_s: float = 8.0
    ) -> str:
        """``resources/read`` of one ``ui://`` document as text (M31, MCP Apps host).

        Raises ``McpFailure``/``ConnectionRequired``; the text is capped at 512 KiB.
        """
        phase: Phase = "connect"
        try:
            async with asyncio.timeout(timeout_s):
                async with self._client(plugin, user_id, timeout_s) as client:
                    phase = "call"
                    result = await client.read_resource(uri)
        except (ConnectionRequired, McpFailure):
            raise
        except Exception as error:
            raise _logged(_classify(error, phase), plugin, error) from error
        text = "".join(str(getattr(item, "text", "") or "") for item in result.contents)
        return text[:524_288]
