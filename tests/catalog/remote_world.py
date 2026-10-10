"""A remote MCP server behind OAuth, plus its test authorization server.

Everything is served from in-process ASGI apps keyed by ``Host``, so the product code
reaches them through the real ``GuardedTransport`` (fake DNS resolver, pinned address):

- ``mcp.test.example``  ``/mcp`` Streamable HTTP MCP server and its protected resource metadata
- ``auth.test.example`` authorization server: metadata, dynamic registration, authorize
  (PKCE S256 + ``resource`` indicator), token (code, rotating refresh), revocation (RFC 7009)
- ``big.test.example``  ``/mcp`` a Linear-sized MCP server (76 tools, no auth; M15-R1)

The authorization server enforces what Mia must send: S256 challenge, exact ``redirect_uri``,
the MCP URL as ``resource`` on authorize, token and refresh requests, and a single-use code.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import urllib.parse
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

MCP_HOST = "mcp.test.example"
AS_HOST = "auth.test.example"
BIG_HOST = "big.test.example"
BIG_URL = f"https://{BIG_HOST}/mcp"
PUBLIC_IP = "93.184.216.34"
RESOURCE = f"https://{MCP_HOST}/mcp"
ISSUER = f"https://{AS_HOST}"
PRM_PATH = "/.well-known/oauth-protected-resource/mcp"


class FakeClock:
    """Seconds since the epoch, advanced by tests; shared by Mia and the authorization server."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeResolver:
    def __init__(self, mapping: Mapping[str, Sequence[str]] | None = None) -> None:
        self.mapping = dict(
            mapping or {MCP_HOST: [PUBLIC_IP], AS_HOST: [PUBLIC_IP], BIG_HOST: [PUBLIC_IP]}
        )
        self.lookups: list[str] = []

    async def __call__(self, host: str, port: int) -> Sequence[str]:
        self.lookups.append(host)
        answer = self.mapping.get(host)
        if not answer:
            raise OSError("NXDOMAIN")
        return answer


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@dataclass
class AuthorizationServer:
    clock: FakeClock
    access_ttl: int = 3600
    issuer: str = ISSUER
    resource: str = RESOURCE
    supports_pkce: bool = True
    supports_registration: bool = True
    supports_revocation: bool = True
    metadata_overrides: dict[str, Any] = field(default_factory=dict)
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    access: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh: dict[str, dict[str, Any]] = field(default_factory=dict)
    authorize_requests: list[dict[str, str]] = field(default_factory=list)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    revocations: list[dict[str, str]] = field(default_factory=list)
    registrations: list[dict[str, Any]] = field(default_factory=list)
    refresh_failures: int = 0
    confidential: str | None = None
    """``client_secret_basic`` / ``client_secret_post``: register confidential clients."""
    secret_failures: int = 0

    # -- helpers used by the resource server and tests ---------------------------------

    def access_valid(self, token: str, resource: str) -> bool:
        record = self.access.get(token)
        return (
            record is not None
            and not record["revoked"]
            and record["resource"] == resource
            and record["expires"] > self.clock()
        )

    def mint_token(self) -> str:
        """A valid access token without a user flow (for publication-time discovery)."""
        return str(self._issue("publisher", "")["access_token"])

    def live_tokens(self) -> list[str]:
        return [t for t, r in self.access.items() if not r["revoked"]]

    # -- endpoints --------------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
        if self.supports_pkce:
            body["code_challenge_methods_supported"] = ["S256"]
        if self.supports_registration:
            body["registration_endpoint"] = f"{self.issuer}/register"
        if self.supports_revocation:
            body["revocation_endpoint"] = f"{self.issuer}/revoke"
        body.update(self.metadata_overrides)
        return body

    async def handle(self, request: Request) -> Response:
        path, method = request.url.path, request.method
        if path == "/.well-known/oauth-authorization-server" and method == "GET":
            return JSONResponse(self.metadata())
        if path == "/register" and method == "POST":
            return await self._register(request)
        if path == "/authorize" and method == "GET":
            return self._authorize(request)
        if path == "/token" and method == "POST":
            return await self._token(request)
        if path == "/revoke" and method == "POST":
            return await self._revoke(request)
        return Response(status_code=404)

    async def _register(self, request: Request) -> Response:
        body = await request.json()
        self.registrations.append(body)
        client_id = "client-" + secrets.token_hex(6)
        self.clients[client_id] = {"redirect_uris": body["redirect_uris"]}
        reply: dict[str, Any] = {
            "client_id": client_id,
            "redirect_uris": body["redirect_uris"],
            "token_endpoint_auth_method": "none",
        }
        if self.confidential:
            secret = "secret-" + secrets.token_hex(8)
            self.clients[client_id]["secret"] = secret
            reply.update(client_secret=secret, token_endpoint_auth_method=self.confidential)
        return JSONResponse(reply, status_code=201)

    def _authorize(self, request: Request) -> Response:
        query = dict(request.query_params)
        self.authorize_requests.append(query)
        client = self.clients.get(query.get("client_id", ""))
        if client is None or query.get("redirect_uri") not in client["redirect_uris"]:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if query.get("response_type") != "code" or query.get("code_challenge_method") != "S256":
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if query.get("resource") != self.resource:
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "client_id": query["client_id"],
            "redirect_uri": query["redirect_uri"],
            "challenge": query["code_challenge"],
            "resource": query["resource"],
            "scope": query.get("scope", ""),
        }
        target = (
            query["redirect_uri"]
            + "?"
            + urllib.parse.urlencode({"code": code, "state": query.get("state", "")})
        )
        return Response(status_code=302, headers={"Location": target})

    async def _token(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        self.token_requests.append(form)
        grant = form.get("grant_type")
        if self.confidential:
            presented = self._presented_secret(request, form)
            client = self.clients.get(form.get("client_id", "") or self._basic_user(request))
            if client is None or presented != client.get("secret"):
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            form.setdefault("client_id", self._basic_user(request))
        if form.get("resource") != self.resource:
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        if grant == "authorization_code":
            record = self.codes.pop(form.get("code", ""), None)  # single use
            ok = (
                record is not None
                and record["client_id"] == form.get("client_id")
                and record["redirect_uri"] == form.get("redirect_uri")
                and _challenge(form.get("code_verifier", "")) == record["challenge"]
            )
            if not ok or record is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(self._issue(record["client_id"], record["scope"]))
        if grant == "refresh_token":
            if self.refresh_failures:
                self.refresh_failures -= 1
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            record = self.refresh.pop(form.get("refresh_token", ""), None)  # rotates
            if record is None or record["revoked"] or record["client_id"] != form.get("client_id"):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(self._issue(record["client_id"], record["scope"]))
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    @staticmethod
    def _basic_user(request: Request) -> str:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return ""
        return base64.b64decode(header[6:]).decode().partition(":")[0]

    @staticmethod
    def _presented_secret(request: Request, form: dict[str, str]) -> str | None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            return base64.b64decode(header[6:]).decode().partition(":")[2]
        return form.get("client_secret")

    def _issue(self, client_id: str, scope: str) -> dict[str, Any]:
        access, refresh = "at-" + secrets.token_hex(8), "rt-" + secrets.token_hex(8)
        self.access[access] = {
            "resource": self.resource,
            "expires": self.clock() + self.access_ttl,
            "revoked": False,
            "refresh": refresh,
        }
        self.refresh[refresh] = {"client_id": client_id, "scope": scope, "revoked": False}
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.access_ttl,
            "refresh_token": refresh,
            "scope": scope,
        }

    async def _revoke(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        self.revocations.append(form)
        token = form.get("token", "")
        if token in self.access:
            self.access[token]["revoked"] = True
            linked = self.refresh.get(self.access[token]["refresh"])
            if linked is not None:
                linked["revoked"] = True
        if token in self.refresh:
            self.refresh[token]["revoked"] = True
            for record in self.access.values():
                if record["refresh"] == token:
                    record["revoked"] = True
        return Response(status_code=200)


def build_mcp_server(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    camel_case_tool: bool = True,
    annotated: bool = False,
) -> MCPServer:
    """A small issue tracker; ``create_issue`` and ``delete_issue`` change things."""
    server = MCPServer("test-tracker", version="1.0.0")

    async def list_issues(ctx: Context[Any, Any]) -> CallToolResult:
        calls.append(("list_issues", {}))
        return CallToolResult(
            content=[TextContent(text="2 open issues")],
            structured_content={"issues": [{"id": "A-1"}, {"id": "A-2"}]},
        )

    async def create_issue(title: str, ctx: Context[Any, Any]) -> CallToolResult:
        calls.append(("create_issue", {"title": title}))
        return CallToolResult(
            content=[TextContent(text=f"Created {title}")], structured_content={"id": "A-3"}
        )

    async def get_issue(issue_id: str, ctx: Context[Any, Any]) -> CallToolResult:
        calls.append(("getIssue", {"issue_id": issue_id}))
        return CallToolResult(content=[TextContent(text=f"Issue {issue_id}")])

    # M72: an annotated server says which tools only read (OI-LIVE-002).
    read_only = ToolAnnotations(read_only_hint=True) if annotated else None
    changes = ToolAnnotations(destructive_hint=False) if annotated else None
    server.add_tool(list_issues, description="List open issues.", annotations=read_only)
    if camel_case_tool:
        # camelCase: a name the manifest grammar cannot hold, so Mia leaves it out.
        server.add_tool(get_issue, name="getIssue", description="Get one issue.")
    server.add_tool(create_issue, description="Create an issue with a title.", annotations=changes)
    return server


_BIG_ENTITIES = (
    "issue",
    "project",
    "comment",
    "document",
    "cycle",
    "label",
    "team",
    "user",
    "milestone",
    "initiative",
    "attachment",
    "customer",
)
_BIG_EXTRA = (
    "assign_issue",
    "move_issue",
    "link_issue",
    "subscribe_issue",
    "set_estimate",
    "duplicate_issue",
    "export_project",
    "import_issues",
)


def big_tool_names() -> list[str]:
    """76 tools shaped like Linear's: reads, main writes and destructive ones."""
    names = [f"list_{entity}s" for entity in _BIG_ENTITIES]
    names += [f"get_{entity}" for entity in _BIG_ENTITIES]
    names += ["search_issues", "search_documentation"]
    names += [f"create_{entity}" for entity in _BIG_ENTITIES]
    names += [f"update_{entity}" for entity in _BIG_ENTITIES]
    names += [f"delete_{entity}" for entity in _BIG_ENTITIES]
    names += [f"archive_{entity}" for entity in _BIG_ENTITIES[:6]]
    names += list(_BIG_EXTRA)
    assert len(names) == 76 and len(set(names)) == 76
    return names


def build_big_server(calls: list[tuple[str, dict[str, Any]]]) -> MCPServer:
    server = MCPServer("big-tracker", version="1.0.0")

    def make(name: str) -> Callable[..., Any]:
        async def tool(query: str = "") -> CallToolResult:
            calls.append((name, {"query": query}))
            return CallToolResult(
                content=[TextContent(text=f"{name} done")], structured_content={"tool": name}
            )

        return tool

    for name in big_tool_names():
        server.add_tool(make(name), name=name, description=f"{name.replace('_', ' ')}.")
    return server


class RemoteWorld:
    """Dispatches by ``Host`` to the MCP resource server or the authorization server."""

    def __init__(
        self,
        auth: AuthorizationServer,
        *,
        require_auth: bool = True,
        camel_case_tool: bool = True,
        annotated: bool = False,
    ) -> None:
        self.auth = auth
        self.require_auth = require_auth
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.mcp = build_mcp_server(
            self.calls, camel_case_tool=camel_case_tool, annotated=annotated
        )
        self.mcp_app: ASGIApp = self.mcp.streamable_http_app(
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(allowed_hosts=[MCP_HOST]),
        )
        self.mcp_requests: list[dict[str, str]] = []
        self.probe_status = 401
        self.big_calls: list[tuple[str, dict[str, Any]]] = []
        self.big = build_big_server(self.big_calls)
        self.big_app: ASGIApp = self.big.streamable_http_app(
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(allowed_hosts=[BIG_HOST]),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.mcp_app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        host = headers.get("host", "")
        if host == AS_HOST:
            request = Request(scope, receive)
            response = await self.auth.handle(request)
            await response(scope, receive, send)
            return
        if host == MCP_HOST:
            await self._resource(scope, receive, send, headers)
            return
        if host == BIG_HOST:
            if scope["path"] == "/mcp":
                await self.big_app(scope, receive, send)
            else:
                await Response(status_code=404)(scope, receive, send)
            return
        await Response(status_code=404)(scope, receive, send)

    async def _resource(
        self, scope: Scope, receive: Receive, send: Send, headers: dict[str, str]
    ) -> None:
        path = scope["path"]
        if path == PRM_PATH:
            body = {
                "resource": RESOURCE,
                "authorization_servers": [self.auth.issuer],
                "scopes_supported": ["issues"],
            }
            await JSONResponse(body)(scope, receive, send)
            return
        self.mcp_requests.append(headers)
        if self.require_auth:
            bearer = headers.get("authorization", "")
            token = bearer.removeprefix("Bearer ") if bearer.startswith("Bearer ") else ""
            if not self.auth.access_valid(token, RESOURCE):
                challenge = (
                    f'Bearer realm="mcp", resource_metadata="https://{MCP_HOST}{PRM_PATH}", '
                    'scope="issues"'
                )
                await Response(
                    status_code=self.probe_status, headers={"WWW-Authenticate": challenge}
                )(scope, receive, send)
                return
        await self.mcp_app(scope, receive, send)


@asynccontextmanager
async def running(world: RemoteWorld) -> AsyncIterator[RemoteWorld]:
    """Run the MCP session manager in its own task (cancel scopes must stay in one task)."""
    ready, stop = asyncio.Event(), asyncio.Event()

    async def runner() -> None:
        async with world.mcp.session_manager.run(), world.big.session_manager.run():
            ready.set()
            await stop.wait()

    task = asyncio.create_task(runner())
    await ready.wait()
    try:
        yield world
    finally:
        stop.set()
        await task


def transport_factory(world: RemoteWorld) -> Callable[[], httpx2.AsyncBaseTransport]:
    return lambda: httpx2.ASGITransport(app=world)


async def browser_authorize(world: RemoteWorld, authorization_url: str) -> str:
    """What the user's browser does: open the URL, return the redirect target (with code)."""
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=world), follow_redirects=False
    ) as client:
        response = await client.get(authorization_url, headers={"Host": AS_HOST})
    assert response.status_code == 302, response.text
    return response.headers["location"]


def callback_parts(location: str) -> tuple[str, str]:
    """``(path?query for our app, ...)``: strip the origin from the redirect target."""
    parts = urllib.parse.urlsplit(location)
    return parts.path, parts.query


def json_dump(value: object) -> str:
    return json.dumps(value, sort_keys=True)
