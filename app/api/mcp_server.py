"""``/mcp``: Mia as a Streamable HTTP MCP server (2026-07-28) for Claude, ChatGPT, Cursor.

Authorization follows MCP: a request without a valid app token gets 401 with
``WWW-Authenticate: Bearer resource_metadata=…/.well-known/oauth-protected-resource/mcp``;
tokens must be issued for the resource ``<issuer>/mcp`` (RFC 8707) or without a resource.
What a client sees depends only on the token's live scopes:

- ``memory.search`` — with any ``memory.read:<category>``; facts of those categories only,
  minimized, every read in the memory access log as ``app``;
- ``memory.suggest`` — with ``memory.suggest``; always lands as *suggested*;
- the user's installed plugin tools — with ``tools.use[:<plugin>]``, through the tool router
  (policy, audit, ``write_external`` approvals as usual: the call waits for the user).

``memory.context`` alone exposes nothing here: it only lets the core add memory to model
requests made through Mia, so no raw fact ever leaves through ``/mcp`` without
``memory.read``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.core.ports.approval_service import ApprovalService
from app.core.ports.memory_provider import MemorySource
from app.core.ports.tool_router import ToolCallRequest, ToolContext, ToolDescriptor, ToolRouter
from app.modules.memory.domain import CATEGORIES
from app.modules.memory.service import MemoryService
from app.modules.oauth.errors import APP_ERROR_CODES, AppApiError
from app.modules.oauth.principal import AppAuthenticator
from app.modules.oauth.scopes import plugins_allowed, readable_categories
from app.modules.oauth.service import AppAccess

SCOPE_KEY: Final = "mia.app_access"
MEMORY_SEARCH: Final = "memory.search"
MEMORY_SUGGEST: Final = "memory.suggest"
SEARCH_SCHEMA: Final = {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500}},
    "required": ["query"],
    "additionalProperties": False,
}
SUGGEST_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        "category": {"type": "string", "enum": list(CATEGORIES)},
    },
    "required": ["text", "category"],
    "additionalProperties": False,
}


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=text)], is_error=True)


@dataclass
class MiaMcpTools:
    router: ToolRouter | None
    approvals: ApprovalService | None
    memory: MemoryService | None

    @staticmethod
    def _context(access: AppAccess, run_id: str) -> ToolContext:
        return ToolContext(
            user_id=str(access.user_id),
            run_id=run_id,
            profile="app",
            app_client_id=access.client_id,
            scopes=access.scopes,
        )

    async def _plugin_tools(self, access: AppAccess) -> list[ToolDescriptor]:
        if self.router is None:
            return []
        allowed = plugins_allowed(access.scopes)
        if allowed is not None and not allowed:
            return []
        listed = await self.router.list_tools(self._context(access, str(uuid.uuid4())))
        return [
            d
            for d in listed
            if d.plugin_id is not None
            and d.executor == "server"
            and (allowed is None or d.plugin_id in allowed)
        ]

    async def list_tools(self, access: AppAccess) -> list[Tool]:
        tools: list[Tool] = []
        if self.memory is not None and readable_categories(access.scopes):
            tools.append(
                Tool(
                    name=MEMORY_SEARCH,
                    description=(
                        "Search what Mia remembers about the user (categories: "
                        + ", ".join(sorted(readable_categories(access.scopes)))
                        + ")."
                    ),
                    input_schema=SEARCH_SCHEMA,
                    annotations=ToolAnnotations(read_only_hint=True),
                )
            )
        if self.memory is not None and "memory.suggest" in access.scopes:
            tools.append(
                Tool(
                    name=MEMORY_SUGGEST,
                    description="Suggest a fact for the user's Mia memory; the user confirms it.",
                    input_schema=SUGGEST_SCHEMA,
                    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
                )
            )
        for d in await self._plugin_tools(access):
            tools.append(
                Tool(
                    name=d.spec.name,
                    description=d.spec.description,
                    input_schema=dict(d.spec.input_schema),
                    annotations=ToolAnnotations(
                        read_only_hint=d.risk == "read",
                        destructive_hint=d.risk == "write_external",
                    ),
                )
            )
        return tools

    async def call(self, access: AppAccess, name: str, arguments: dict[str, Any]) -> CallToolResult:
        if name == MEMORY_SEARCH:
            return await self._search(access, arguments)
        if name == MEMORY_SUGGEST:
            return await self._suggest(access, arguments)
        return await self._plugin_call(access, name, arguments)

    async def _search(self, access: AppAccess, arguments: dict[str, Any]) -> CallToolResult:
        categories = readable_categories(access.scopes)
        query = arguments.get("query")
        if self.memory is None or not categories:
            return _error("Not permitted: memory.read:<category> is required.")
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            return _error("query must be a non-empty string.")
        records = await self.memory.context(
            user_id=str(access.user_id),
            query=query,
            categories=categories,
            actor="app",
            app_client_id=access.client_id,
        )
        facts = [
            {"category": r.category, "text": r.text} for r in records if r.category in categories
        ]
        text = "\n".join(f"- [{f['category']}] {f['text']}" for f in facts) or "Nothing found."
        return CallToolResult(content=[TextContent(text=text)], structured_content={"facts": facts})

    async def _suggest(self, access: AppAccess, arguments: dict[str, Any]) -> CallToolResult:
        if self.memory is None or "memory.suggest" not in access.scopes:
            return _error("Not permitted: memory.suggest is required.")
        text, category = arguments.get("text"), arguments.get("category")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return _error("text must be 1-2000 characters.")
        if category not in CATEGORIES:
            return _error("Unknown category.")
        fact = await self.memory.remember(
            user_id=str(access.user_id),
            text=text,
            category=category,
            source=MemorySource(type="app", ref=access.client_id),
            actor="app",
            app_client_id=access.client_id,
            force_suggested=True,
        )
        return CallToolResult(
            content=[TextContent(text="Suggested; the user will confirm it in Mia.")],
            structured_content={"fact_id": str(fact.id), "status": fact.status},
        )

    async def _plugin_call(
        self, access: AppAccess, name: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        if self.router is None:
            return _error("Unknown tool.")
        offered = {d.spec.name for d in await self._plugin_tools(access)}
        if name not in offered:
            return _error("Unknown tool or not permitted for this app.")
        context = self._context(access, str(uuid.uuid4()))
        request = ToolCallRequest(call_id=f"mcp_{uuid.uuid4().hex}", tool=name, arguments=arguments)
        outcome = await self.router.call(context, request)
        if outcome.status == "approval_required" and outcome.approval is not None:
            if self.approvals is None:
                return _error("Approval is unavailable.")
            approval = outcome.approval
            remaining = (approval.expires_at - datetime.now(UTC)).total_seconds()
            current = await self.approvals.wait(
                user_id=context.user_id, approval_id=approval.id, timeout_s=max(0.0, remaining)
            )
            if current.status != "approved":
                status = current.status if current.status != "pending" else "expired"
                return _error(f"The user did not approve this action ({status}).")
            outcome = await self.router.call(
                context,
                ToolCallRequest(
                    call_id=request.call_id,
                    tool=name,
                    arguments=arguments,
                    approval_id=approval.id,
                ),
            )
        structured = dict(outcome.structured_content) if outcome.structured_content else None
        if outcome.status != "succeeded":
            return CallToolResult(
                content=[
                    TextContent(
                        text=json.dumps(
                            {"status": outcome.status, "error": outcome.error_code or "failed"}
                        )
                    )
                ],
                is_error=True,
            )
        return CallToolResult(
            content=[TextContent(text=outcome.content or "OK")], structured_content=structured
        )


def _access(ctx: Any) -> AppAccess:
    request = getattr(ctx, "request", None)
    scope = getattr(request, "scope", None) or {}
    access = scope.get(SCOPE_KEY)
    if not isinstance(access, AppAccess):
        raise RuntimeError("unauthenticated MCP request reached the server")
    return access


def build_mcp_server(tools: MiaMcpTools) -> Server[Any]:
    async def on_list_tools(ctx: Any, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=await tools.list_tools(_access(ctx)))

    async def on_call_tool(ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        return await tools.call(_access(ctx), params.name, dict(params.arguments or {}))

    return Server(
        "mia",
        version="1",
        title="Mia",
        instructions="Mia: the user's memory and installed plugins within the granted scopes.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


class MiaMcpEndpoint:
    """ASGI app for ``/mcp``: authenticate the app token, then hand over to the MCP SDK."""

    def __init__(self, tools: MiaMcpTools, apps: AppAuthenticator) -> None:
        self.apps = apps
        # Every request carries a bearer token checked above, so there are no ambient
        # credentials for DNS rebinding to abuse; a Host allow-list would also break behind
        # a reverse proxy that rewrites Host.
        self.manager = StreamableHTTPSessionManager(
            app=build_mcp_server(tools),
            json_response=True,
            stateless=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with self.manager.run():
            yield

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        try:
            access = await self.apps.authenticate(request, mcp=True)
        except AppApiError as error:
            retryable = APP_ERROR_CODES.get(error.code, (0, False, ""))[1]
            response = JSONResponse(
                status_code=error.status,
                headers=error.headers,
                content={
                    "error": {"code": error.code, "message": error.message, "retryable": retryable}
                },
            )
            await response(scope, receive, send)
            return
        scope[SCOPE_KEY] = access
        await self.manager.handle_request(scope, receive, send)
