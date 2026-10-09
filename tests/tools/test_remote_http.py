"""Remote MCP over Streamable HTTP (local ASGI server) and the SSRF guard."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from app.core.ports.tool_router import ToolCallRequest, ToolContext
from app.modules.plugins.registry import (
    InstallationRecord,
    ManifestError,
    PluginRegistry,
    StaticInstallationReader,
    parse_manifest,
)
from app.modules.plugins.registry.manifest import RemoteAuth
from app.modules.tools.approvals import InMemoryApprovalService
from app.modules.tools.audit import InMemoryToolAudit
from app.modules.tools.mcp_client import McpConnector
from app.modules.tools.netguard import (
    GuardedTransport,
    HostBlockedError,
    HostPolicy,
    is_public_address,
    vetted_address,
)
from app.modules.tools.router import McpToolRouter, RouterConfig
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from tests.tools.helpers import Recorder, build_server, digest_of, manifest

HOST = "mcp.test.example"
PUBLIC_IP = "93.184.216.34"
PLUGIN = "remote.timer"


class FakeResolver:
    def __init__(self, answers: Mapping[str, Sequence[Sequence[str]]]) -> None:
        self.answers = {host: list(seq) for host, seq in answers.items()}
        self.lookups: list[str] = []

    async def __call__(self, host: str, port: int) -> Sequence[str]:
        self.lookups.append(host)
        sequence = self.answers.get(host)
        if not sequence:
            raise OSError("NXDOMAIN")
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]


@dataclass
class HeaderSpy:
    app: ASGIApp
    seen: list[dict[str, str]] = field(default_factory=list)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.seen.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await self.app(scope, receive, send)


class Credentials:
    def __init__(self, token: str | None) -> None:
        self.token = token

    async def headers(
        self, *, user_id: str, installation_id: str, plugin_id: str, auth: RemoteAuth
    ) -> Mapping[str, str] | None:
        return {"Authorization": f"Bearer {self.token}"} if self.token else None


@dataclass
class Remote:
    router: McpToolRouter
    recorder: Recorder
    spy: HeaderSpy
    resolver: FakeResolver
    user_id: str
    approvals: InMemoryApprovalService

    def context(self) -> ToolContext:
        return ToolContext(user_id=self.user_id, run_id=str(uuid.uuid4()), profile="chat")


@asynccontextmanager
async def remote_world(
    *,
    answers: Mapping[str, Sequence[Sequence[str]]] | None = None,
    auth: str = "none",
    token: str | None = None,
    allowlist: Sequence[str] | None = None,
    url: str = f"https://{HOST}/mcp",
    call_timeout_s: float = 0.5,
    user_id: str | None = None,
) -> AsyncIterator[Remote]:
    recorder = Recorder()
    server = build_server(recorder)
    spy = HeaderSpy(
        server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(allowed_hosts=[HOST]),
        )
    )
    body = manifest(
        PLUGIN,
        server={"kind": "remote", "url": url, "auth": auth, "protocolVersion": "2026-07-28"},
        network=[HOST],
    )
    user_id = user_id or str(uuid.uuid4())
    installation = InstallationRecord(
        id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        plugin_id=PLUGIN,
        version="1.0.0",
        status="active",
        grants=(),
        tools_digest=await digest_of(server),
        manifest=body,
    )
    resolver = FakeResolver(answers or {HOST: [[PUBLIC_IP]]})
    connector = McpConnector(
        resolver=resolver,
        http_transport=lambda: httpx2.ASGITransport(app=spy),
        credentials=Credentials(token),
        global_allowlist=allowlist,
    )
    approvals = InMemoryApprovalService()
    router = McpToolRouter(
        registry=PluginRegistry(StaticInstallationReader({user_id: [installation]})),
        connector=connector,
        audit=InMemoryToolAudit(),
        approvals=approvals,
        config=RouterConfig(call_timeout_s=call_timeout_s, list_cache_s=0),
    )
    async with server.session_manager.run():
        yield Remote(router, recorder, spy, resolver, user_id, approvals)


def req(tool: str, arguments: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(call_id="call_remote", tool=f"{PLUGIN}.{tool}", arguments=arguments)


async def test_streamable_http_list_and_call_with_minimal_meta() -> None:
    async with remote_world() as world:
        names = {d.spec.name for d in await world.router.list_tools(world.context())}
        assert f"{PLUGIN}.start" in names
        outcome = await world.router.call(world.context(), req("start", {"seconds": 30}))

    assert outcome.status == "succeeded"
    assert outcome.structured_content == {"timer": {"seconds": 30, "state": "running"}}
    _, _, meta = world.recorder.calls[0]
    mia_keys = {key for key in meta if key.startswith("mia/")}
    assert mia_keys == {"mia/installation_id", "mia/operation_id"}
    # Every request was pinned to the vetted address but kept the Host name.
    assert world.spy.seen and all(h["host"] == HOST for h in world.spy.seen)
    assert set(world.resolver.lookups) == {HOST}


async def test_private_address_is_blocked_before_any_request() -> None:
    async with remote_world(answers={HOST: [["10.0.0.5"]]}) as world:
        assert await world.router.list_tools(world.context()) == []
        outcome = await world.router.call(world.context(), req("start", {"seconds": 1}))
    assert (outcome.status, outcome.error_code) == ("failed", "tool_host_blocked")
    assert world.spy.seen == [] and world.recorder.calls == []


@pytest.mark.parametrize(
    "addresses",
    [
        [PUBLIC_IP, "127.0.0.1"],
        ["169.254.169.254"],
        ["fd00:ec2::254"],
        ["::ffff:10.0.0.1"],
        ["100.64.1.1"],
        ["::1"],
    ],
)
async def test_rebinding_and_metadata_answers_are_refused(addresses: list[str]) -> None:
    async with remote_world(answers={HOST: [addresses]}) as world:
        outcome = await world.router.call(world.context(), req("start", {"seconds": 1}))
    assert outcome.error_code == "tool_host_blocked" and world.spy.seen == []


async def test_dns_rebinding_between_requests_is_caught_per_request() -> None:
    # First resolution public (list works), later ones private: the call is refused.
    async with remote_world(answers={HOST: [[PUBLIC_IP], [PUBLIC_IP], ["192.168.1.10"]]}) as world:
        router = world.router
        listed = await router.list_tools(world.context())
        assert listed
        outcome = await router.call(world.context(), req("start", {"seconds": 1}))
    assert outcome.status == "failed" and outcome.error_code in {
        "tool_host_blocked",
        "tool_unavailable",
    }
    assert world.recorder.calls == []


async def test_global_allowlist_narrows_hosts() -> None:
    async with remote_world(allowlist=["other.example"]) as world:
        outcome = await world.router.call(world.context(), req("start", {"seconds": 1}))
    assert outcome.error_code == "tool_host_blocked" and world.spy.seen == []


async def test_oauth_plugin_needs_a_connection_and_token_stays_out_of_meta() -> None:
    async with remote_world(auth="oauth") as world:
        missing = await world.router.call(world.context(), req("start", {"seconds": 1}))
    assert (missing.status, missing.error_code) == ("failed", "connection_required")

    async with remote_world(auth="oauth", token="tok-123") as world:
        ok = await world.router.call(world.context(), req("start", {"seconds": 1}))
    assert ok.status == "succeeded"
    assert all(h.get("authorization") == "Bearer tok-123" for h in world.spy.seen)
    _, _, meta = world.recorder.calls[0]
    assert "tok-123" not in repr(meta)


async def test_remote_write_timeout_is_uncertain_once() -> None:
    async with remote_world(call_timeout_s=0.3) as world:
        outcome = await world.router.call(world.context(), req("slow_write", {}))
    assert (outcome.status, outcome.error_code) == ("uncertain", "tool_timeout")
    assert world.recorder.names() == ["slow_write"]


# -- guard units ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "public"),
    [
        (PUBLIC_IP, True),
        ("2606:4700:4700::1111", True),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.0.1", False),
        ("127.0.0.1", False),
        ("0.0.0.0", False),
        ("169.254.169.254", False),
        ("100.100.100.200", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:127.0.0.1", False),
        ("224.0.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_public_address_classification(address: str, public: bool) -> None:
    assert is_public_address(address) is public


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.test.example/mcp",
        "https://user:pw@mcp.test.example/mcp",
        "https://93.184.216.34/mcp",
        "https://[2606:4700::1111]/mcp",
        "https://evil.example/mcp",
    ],
)
def test_host_policy_refuses(url: str) -> None:
    with pytest.raises(HostBlockedError):
        HostPolicy([HOST]).check(httpx2.URL(url))


async def test_vetted_address_requires_all_answers_public() -> None:
    assert await vetted_address(HOST, 443, FakeResolver({HOST: [[PUBLIC_IP]]})) == PUBLIC_IP
    for answers in ([PUBLIC_IP, "10.0.0.1"], []):
        with pytest.raises(HostBlockedError):
            await vetted_address(HOST, 443, FakeResolver({HOST: [answers]}))
    with pytest.raises(HostBlockedError):
        await vetted_address("nx.example", 443, FakeResolver({}))


async def test_guarded_transport_pins_ip_and_keeps_sni_and_host() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == "/hop":
            return httpx2.Response(307, headers={"location": "https://internal.example/x"})
        return httpx2.Response(200)

    transport = GuardedTransport(
        HostPolicy([HOST]),
        resolver=FakeResolver({HOST: [[PUBLIC_IP]], "internal.example": [["10.0.0.1"]]}),
        inner=httpx2.MockTransport(handler),
    )
    async with httpx2.AsyncClient(transport=transport, follow_redirects=True) as client:
        response = await client.get(f"https://{HOST}/ok")
        assert response.status_code == 200
        with pytest.raises(HostBlockedError):
            await client.get(f"https://{HOST}/hop")
    first = seen[0]
    assert first.url.host == PUBLIC_IP and first.headers["host"] == HOST
    assert first.extensions["sni_hostname"] == HOST
    assert len(seen) == 2  # the redirect target was never contacted


def test_remote_manifest_host_must_be_in_permissions_network(tmp_path: Path) -> None:
    body = manifest(
        PLUGIN,
        server={"kind": "remote", "url": "https://elsewhere.example/mcp", "auth": "none"},
        network=[HOST],
    )
    with pytest.raises(ManifestError):
        parse_manifest(body)
