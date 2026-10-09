"""MW2 gate: the model calls the timer through MCP and the router; approvals work.

Production wiring of M10 (auth, workspace), M11 (gateway, ``chatgpt-plan``), M12 (runs),
M13 (router, approvals) and M14 (builtin plugins): a signed-in Android user installs
``builtin.timer`` from the catalog with the consented digest, the primary model (a
recorded Responses stream) calls the aliased ``builtin.timer.start``, the router reaches
the M14 server in process and the timer lands in ``PluginState``. A ``write_external``
tool waits for ``POST /v1/approvals/{id}/decision`` over HTTP.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from app.composition import AuthServiceAuthenticator
from app.core.config import Settings
from app.core.crypto import MasterKeyring, generate_master_key
from app.core.database import Database
from app.core.redis import RedisClient
from app.core.runtime import Runtime
from app.main import create_app
from app.modules.auth.cache import RedisAuthCache
from app.modules.auth.composition import install_auth
from app.modules.auth.service import AuthService
from app.modules.auth.tokens import TokenManager
from app.modules.models.adapters import AdapterSettings
from app.modules.models.adapters.tool_names import ToolNames
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.composition import build_models_stack
from app.modules.plugins.composition import BuiltinPlugins, build_builtin_plugins
from app.modules.plugins.registry import DirectoryManifestSource
from app.modules.runs.composition import install_runs
from app.modules.tools.composition import BUILTIN_PLUGINS_DIR, install_tools
from app.modules.workspace.catalog import CatalogPlugin, PluginCatalog
from app.modules.workspace.composition import install_workspace
from mcp.server.mcpserver import MCPServer
from sqlalchemy import text

from tests.auth.fakes import FakeRedis
from tests.fakes import assert_mvp_valid
from tests.models.auth_world import CLIENT_ID as PLAN_CLIENT_ID
from tests.tools.helpers import Recorder, build_server, digest_of, manifest, write_package
from tests.workspace.harness import (
    BASE_URL,
    CLIENT_ID,
    SECRET,
    TEST_DATABASE_URL,
    TokenGoogleVerifier,
    android_login,
    key,
    migrate_to_head,
)

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
TIMER = "builtin.timer"
MAILER = "gate.mailer"


async def _no_sleep(_seconds: float) -> None:
    return None


def _sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode() for e in events)


class ScriptedResponses:
    """Responses API stream per step: tool call(s) first, then a final JSON answer."""

    def __init__(self, steps: list[dict[str, Any] | str]) -> None:
        self.steps = steps
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        step = self.steps[len(self.bodies) - 1]
        if isinstance(step, dict):
            names = {tool["name"] for tool in body.get("tools", [])}
            assert step["name"] in names, (step["name"], names)
            item = {"type": "function_call", "call_id": f"call_{len(self.bodies)}", **step}
            events = [{"type": "response.output_item.done", "item": item}]
        else:
            events = [{"type": "response.output_text.delta", "delta": step}]
        events.append({"type": "response.completed", "response": {"usage": {}}})
        return httpx.Response(
            200, content=_sse(*events), headers={"content-type": "text/event-stream"}
        )


class GateCatalog:
    """The builtin catalog plus one test plugin with a ``write_external`` tool."""

    def __init__(self, builtin: PluginCatalog, extra: CatalogPlugin) -> None:
        self._builtin = builtin
        self._extra = extra

    async def resolve(self, plugin_id: str, version: str) -> CatalogPlugin | None:
        if (plugin_id, version) == (self._extra.plugin_id, self._extra.version):
            return self._extra
        return await self._builtin.resolve(plugin_id, version)


@dataclass
class Gate:
    client: httpx.AsyncClient
    auth: dict[str, str]
    plugins: BuiltinPlugins
    model: ScriptedResponses
    recorder: Recorder
    mailer_digest: str


@pytest.fixture
async def gate(tmp_path: Path) -> AsyncIterator[Gate]:
    await asyncio.to_thread(migrate_to_head)
    assert TEST_DATABASE_URL is not None
    database = Database(TEST_DATABASE_URL)
    async with database.session_factory() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE users CASCADE"))
        await session.execute(text("TRUNCATE TABLE model_gateway_settings"))
    settings = Settings(app_env="test", tools_list_cache_seconds=0)
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

    model = ScriptedResponses([])
    models = build_models_stack(
        settings,
        database.session_factory,
        keyring=MasterKeyring.from_config(f"k1:{generate_master_key()}"),
        transport=httpx.MockTransport(model),
        adapter_settings=AdapterSettings(pause_seconds=3600, sleep=_no_sleep),
    )
    plugins = build_builtin_plugins(runtime, model_gateway=models.gateway)

    recorder = Recorder()
    mailer_server = build_server(recorder)
    mailer_body = manifest(MAILER)
    write_package(tmp_path, mailer_body)
    mailer = CatalogPlugin(
        plugin_id=MAILER,
        version="1.0.0",
        tools_digest=await digest_of(mailer_server),
        manifest=mailer_body,
    )

    def server_factory(raw: Mapping[str, Any], plugin_dir: Path | None) -> MCPServer:
        if raw.get("id") == MAILER:
            return mailer_server
        return plugins.server_factory(raw, plugin_dir)

    install_workspace(app, database=database, catalog=GateCatalog(plugins.catalog, mailer))
    tools = install_tools(
        app,
        settings,
        runtime,
        server_factory=server_factory,
        manifest_source=DirectoryManifestSource(BUILTIN_PLUGINS_DIR, tmp_path),
    )
    runs = install_runs(
        app,
        settings,
        runtime,
        authenticator=AuthServiceAuthenticator(auth_service),
        model_gateway=models.gateway,
        tool_router=tools.router,
        approvals=tools.approvals,
    )
    tools.device_channel.bind_publisher(runs.publish)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as client:
        auth = await android_login(client, "gate-owner")
        async with database.session_factory() as session:
            [user_id] = (await session.scalars(text("SELECT id FROM users"))).all()
        tokens = oauth.TokenSet(PLAN_CLIENT_ID, "access-1", "refresh-1", time.time() + 36_000, [])
        await models.plan_store.save_sign_in(user_id, tokens, model="gpt-6.1-sol")
        yield Gate(client, auth, plugins, model, recorder, mailer.tools_digest)
    await runs.aclose()
    await models.aclose()
    await database.close()
    await redis.close()


async def install(gate: Gate, plugin_id: str, digest: str) -> str:
    response = await gate.client.post(
        "/v1/installations",
        headers={**gate.auth, **key()},
        json={"plugin_id": plugin_id, "version": "1.0.0", "tools_digest": digest},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def start_run(gate: Gate, text_: str, profile: str = "chat") -> str:
    created = await gate.client.post(
        "/v1/runs",
        json={"profile": profile, "input": {"text": text_}},
        headers={**gate.auth, "Idempotency-Key": f"gate-{uuid.uuid4()}"},
    )
    assert created.status_code == 202, created.text
    return str(created.json()["id"])


def frames(body: str) -> list[dict[str, Any]]:
    out = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if not line.startswith(":")
        )
        if fields:
            data = json.loads(fields["data"])
            assert_mvp_valid(data, "run-events")
            out.append(data)
    return out


async def events_of(gate: Gate, run_id: str) -> list[dict[str, Any]]:
    async with asyncio.timeout(15):
        streamed = await gate.client.get(f"/v1/runs/{run_id}/events", headers=gate.auth)
    assert streamed.status_code == 200
    return frames(streamed.text)


FINAL = "Таймер на пять минут запущен."


async def test_primary_model_calls_the_builtin_timer_through_mcp_and_the_router(
    gate: Gate,
) -> None:
    resolved = await gate.plugins.catalog.resolve(TIMER, "1.0.0")
    assert resolved is not None
    installation = await install(gate, TIMER, resolved.tools_digest)
    wire = ToolNames({f"{TIMER}.start"}).wire(f"{TIMER}.start")
    gate.model.steps = [{"name": wire, "arguments": json.dumps({"seconds": 300})}, FINAL]

    run_id = await start_run(gate, "Поставь таймер на пять минут", profile="voice")
    events = await events_of(gate, run_id)
    types = [event["type"] for event in events]
    assert types[0] == "run.started" and types[-1] == "run.completed", types
    started = next(e for e in events if e["type"] == "tool.call.started")["payload"]
    assert started["tool"] == f"{TIMER}.start" and started["plugin_id"] == TIMER
    completed = next(e for e in events if e["type"] == "tool.call.completed")["payload"]
    assert completed["status"] == "succeeded", completed
    assert completed["ui_resource_uri"].startswith("ui://")
    [item] = completed["structured_content"]["items"]
    assert item["status"] == "running" and item["duration_ms"] == 300_000
    final = next(e for e in events if e["type"] == "response.final")["payload"]["response"]
    assert final["speech"] == "Таймер на пять минут запущен."

    # Only valid wire names reached the provider; the result came back as untrusted data.
    first, second = gate.model.bodies
    assert all("." not in tool["name"] for tool in first["tools"])
    replayed = [i for i in second["input"] if i.get("type") == "function_call"]
    assert [i["name"] for i in replayed] == [wire]
    [output] = [i for i in second["input"] if i.get("type") == "function_call_output"]
    assert 'trust="untrusted"' in json.loads(output["output"])["content"]

    state = await gate.client.get(f"/v1/installations/{installation}/state", headers=gate.auth)
    assert state.status_code == 200, state.text
    [stored] = state.json()["data"]["items"]
    assert stored["id"] == item["id"] and stored["deadline_ms"] == item["deadline_ms"]


async def test_external_write_waits_for_an_http_approval(gate: Gate) -> None:
    resolved = await gate.plugins.catalog.resolve(TIMER, "1.0.0")
    assert resolved is not None
    await install(gate, TIMER, resolved.tools_digest)
    await install(gate, MAILER, gate.mailer_digest)
    names = ToolNames({f"{MAILER}.send"})
    gate.model.steps = [
        {"name": names.wire(f"{MAILER}.send"), "arguments": json.dumps({"text": "Привет"})},
        "Отправила.",
    ]

    run_id = await start_run(gate, "Отправь привет")
    approval: dict[str, Any] | None = None
    async with asyncio.timeout(10):
        while approval is None:
            listed = await gate.client.get("/v1/approvals?status=pending", headers=gate.auth)
            assert listed.status_code == 200, listed.text
            items = listed.json()["items"]
            approval = items[0] if items else None
            if approval is None:
                await asyncio.sleep(0.05)
    assert approval["arguments"] == {"text": "Привет"}
    assert gate.recorder.names() == []

    wrong = await gate.client.post(
        f"/v1/approvals/{approval['id']}/decision",
        headers={**gate.auth, **key()},
        json={"decision": "approve", "args_hash": "sha256:" + "0" * 64},
    )
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "approval_args_mismatch"
    decided = await gate.client.post(
        f"/v1/approvals/{approval['id']}/decision",
        headers={**gate.auth, **key()},
        json={"decision": "approve", "args_hash": approval["args_hash"]},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"]["channel"] == "android"

    events = await events_of(gate, run_id)
    types = [event["type"] for event in events]
    assert types[types.index("approval.requested") :][:3] == [
        "approval.requested",
        "approval.resolved",
        "tool.call.completed",
    ]
    completed = next(e for e in events if e["type"] == "tool.call.completed")["payload"]
    assert completed["status"] == "succeeded"
    assert types[-1] == "run.completed"
    assert gate.recorder.names() == ["send"]
