"""``/mcp``: Mia as an MCP server, driven by the MCP SDK client (M50).

DoD: ``memory.context`` without ``memory.read`` never returns raw facts.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from app.modules.memory.models import MemoryAccessLog, MemoryFact
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from tests.oauth.harness import ISSUER, OAuthWorld, oauth_world
from tests.runs.conftest import requires_postgres
from tests.tools.helpers import build_world

pytestmark = requires_postgres
MCP = f"{ISSUER}/mcp"


@pytest.fixture
async def world() -> AsyncIterator[OAuthWorld]:
    async with oauth_world() as built:
        yield built


@pytest.fixture
async def tools_world(tmp_path: Path) -> AsyncIterator[tuple[OAuthWorld, Any]]:
    holder: dict[str, Any] = {}

    async def router(users: list[uuid.UUID]) -> Any:
        holder["world"] = await build_world(tmp_path, user_id=str(users[0]), call_timeout_s=5)
        return holder["world"].router

    async with oauth_world(router_factory=router) as built:
        yield built, holder["world"]


async def token(world: OAuthWorld, scope: str, *, resource: str | None = MCP) -> str:
    async with world.http() as http:
        tokens = await world.login(http, scope=scope, resource=resource)
    return str(tokens["access_token"])


@asynccontextmanager
async def mcp(world: OAuthWorld, access: str) -> AsyncIterator[Client]:
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=world.app),
        headers={"Authorization": f"Bearer {access}"},
        timeout=httpx2.Timeout(30),
    )
    async with http:
        client = Client(
            streamable_http_client(MCP, http_client=http, terminate_on_close=False), cache=None
        )
        async with client:
            yield client


async def facts(world: OAuthWorld) -> None:
    user = str(world.user)
    await world.memory.create_fact(user_id=user, text="Работаю в студии «Сова»", category="work")
    await world.memory.create_fact(user_id=user, text="Люблю горький шоколад", category="profile")


def text_of(result: Any) -> str:
    return "".join(getattr(part, "text", "") for part in result.content)


async def test_unauthenticated_mcp_points_to_the_resource_metadata(world: OAuthWorld) -> None:
    async with world.http() as http:
        response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        api_token = await token(world, "llm.generate", resource=ISSUER)
        wrong = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {api_token}"},
        )
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert wrong.status_code == 401 and 'error="invalid_token"' in wrong.headers["www-authenticate"]


async def test_memory_context_alone_exposes_no_raw_facts(world: OAuthWorld) -> None:
    await facts(world)
    async with mcp(world, await token(world, "memory.context llm.generate")) as client:
        listed = await client.list_tools()
        searched = await client.call_tool("memory.search", {"query": "студии"})
    assert listed.tools == []
    assert searched.is_error
    assert "Сова" not in text_of(searched) and "шоколад" not in text_of(searched)
    async with world.factory() as session:
        app_reads = (
            await session.scalars(select(MemoryAccessLog).where(MemoryAccessLog.actor == "app"))
        ).all()
    assert app_reads == []


async def test_memory_read_returns_only_granted_categories(world: OAuthWorld) -> None:
    await facts(world)
    async with mcp(world, await token(world, "memory.read:work")) as client:
        listed = await client.list_tools()
        found = await client.call_tool("memory.search", {"query": "студии шоколад"})
    assert [t.name for t in listed.tools] == ["memory.search"]
    assert listed.tools[0].annotations is not None and listed.tools[0].annotations.read_only_hint
    assert "Сова" in text_of(found) and "шоколад" not in text_of(found)
    assert found.structured_content == {
        "facts": [{"category": "work", "text": "Работаю в студии «Сова»"}]
    }
    async with world.factory() as session:
        logged = (
            await session.scalars(select(MemoryAccessLog).where(MemoryAccessLog.actor == "app"))
        ).all()
    assert [(r.action, r.app_client_id) for r in logged] == [
        ("context", "https://notes.example/oauth/client.json")
    ]


async def test_memory_suggest_lands_as_suggested(world: OAuthWorld) -> None:
    async with mcp(world, await token(world, "memory.suggest")) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        result = await client.call_tool(
            "memory.suggest", {"text": "Предпочитает тёмную тему", "category": "interests"}
        )
        refused = await client.call_tool("memory.search", {"query": "тему"})
    assert names == ["memory.suggest"]
    assert result.structured_content is not None
    assert result.structured_content["status"] == "suggested"
    assert refused.is_error
    async with world.factory() as session:
        row = (await session.scalars(select(MemoryFact))).one()
    assert (row.status, row.source_type, row.source_ref) == (
        "suggested",
        "app",
        "https://notes.example/oauth/client.json",
    )


async def test_installed_plugin_tools_follow_tools_use_and_approvals(
    tools_world: tuple[OAuthWorld, Any],
) -> None:
    world, plugins = tools_world
    async with mcp(world, await token(world, "memory.context")) as client:
        assert (await client.list_tools()).tools == []
        hidden = await client.call_tool("test.timer.get", {})
    assert hidden.is_error and plugins.recorder.names() == []

    async with mcp(world, await token(world, "tools.use:test.timer")) as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
        got = await client.call_tool("test.timer.get", {})

        async def approve() -> None:
            for _ in range(200):
                pending = await plugins.approvals.list_for_user(
                    user_id=str(world.user), status="pending"
                )
                if pending:
                    await plugins.approvals.decide(
                        user_id=str(world.user),
                        approval_id=pending[0].id,
                        decision="approve",
                        args_hash=pending[0].args_hash,
                        channel="web",
                    )
                    return
                await asyncio.sleep(0.02)

        approver = asyncio.create_task(approve())
        sent = await client.call_tool("test.timer.send", {"text": "Привет"})
        await approver
    assert "test.timer.send" in listed and listed["test.timer.send"].annotations is not None
    assert listed["test.timer.send"].annotations.destructive_hint is True
    # Plugin output stays framed as untrusted data for the client's model, like in Mia's runs.
    assert 'trust="untrusted"' in text_of(got) and "No timers." in text_of(got)
    assert not got.is_error
    assert not sent.is_error and "Sent." in text_of(sent)
    assert plugins.recorder.names() == ["get", "send"]


async def test_denied_approval_executes_nothing(tools_world: tuple[OAuthWorld, Any]) -> None:
    world, plugins = tools_world
    async with mcp(world, await token(world, "tools.use")) as client:

        async def deny() -> None:
            for _ in range(200):
                pending = await plugins.approvals.list_for_user(
                    user_id=str(world.user), status="pending"
                )
                if pending:
                    await plugins.approvals.decide(
                        user_id=str(world.user),
                        approval_id=pending[0].id,
                        decision="deny",
                        args_hash=pending[0].args_hash,
                        channel="web",
                    )
                    return
                await asyncio.sleep(0.02)

        denier = asyncio.create_task(deny())
        result = await client.call_tool("test.timer.send", {"text": "Привет"})
        await denier
    assert result.is_error and "denied" in text_of(result)
    assert "send" not in plugins.recorder.names()


async def test_app_tokens_never_reach_first_party_routes(world: OAuthWorld) -> None:
    """An app token is opaque: the first-party JWT verifier rejects it everywhere."""
    from app.core.config import Settings
    from app.main import create_app

    access = await token(world, "tools.use memory.read:work llm.generate", resource=None)
    app = create_app(Settings(app_env="test"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        headers = {"Authorization": f"Bearer {access}"}
        for path in ("/v1/approvals", "/v1/memory/facts", "/v1/installations", "/v1/apps"):
            response = await http.get(path, headers=headers)
            assert response.status_code == 401, (path, response.text)
            assert json.loads(response.text)["error"]["code"] == "invalid_credentials"
