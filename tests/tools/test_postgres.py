"""PostgreSQL: approvals, audit rows, tenant-scoped installations, approvals API, SQL e2e."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.database import Database
from app.core.ports.approval_service import ApprovalError, ApprovalRequest, canonical_args_hash
from app.core.redis import RedisClient
from app.core.runtime import Runtime
from app.modules.plugins.registry import (
    LoadedManifest,
    PluginRegistry,
    SqlInstallationReader,
    StaticManifestSource,
)
from app.modules.runs import (
    EngineConfig,
    NewRun,
    RunEngine,
    RunNotifier,
    RunService,
    SqlRunStore,
)
from app.modules.tools.approvals import SqlApprovalService
from app.modules.tools.audit import SqlToolAudit
from app.modules.tools.composition import install_tools
from app.modules.tools.mcp_client import McpConnector
from app.modules.tools.models import ToolApproval, ToolExecution
from app.modules.tools.router import McpToolRouter, RouterConfig
from app.modules.workspace.models import Installation
from app.modules.workspace.service import ensure_personal_workspace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes import text_step, tool_step
from tests.runs.conftest import (
    TEST_LIMITS,
    _migrated_factory,
    collect,
    create_users,
    payload_of,
    requires_postgres,
)
from tests.runs.gateways import answer, scripted
from tests.tools.helpers import PLUGIN_ID, Clock, Recorder, build_server, digest_of, manifest
from tests.workspace.harness import (
    android_login,
    build_harness,
    database_url,
    key,
    web_headers,
    web_login,
)

pytestmark = requires_postgres


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    session_factory = await _migrated_factory()
    yield session_factory
    await session_factory.kw["bind"].dispose()


def approval_request(user: uuid.UUID, run_id: uuid.UUID, text: str = "hi") -> ApprovalRequest:
    return ApprovalRequest(
        user_id=str(user),
        run_id=str(run_id),
        tool_call_id="call_1",
        tool=f"{PLUGIN_ID}.send",
        risk="write_external",
        summary="Test timer: send",
        arguments={"text": text},
        plugin_id=PLUGIN_ID,
    )


async def test_sql_approvals_lifecycle(factory: async_sessionmaker[AsyncSession]) -> None:
    owner, stranger = await create_users(factory, 2)
    clock = Clock()
    service = SqlApprovalService(factory, clock=clock, poll_s=0.05)
    run_id = uuid.uuid4()
    approval = await service.request(approval_request(owner, run_id, "Привет"))

    assert approval.args_hash == canonical_args_hash({"text": "Привет"})
    assert await service.get(user_id=str(stranger), approval_id=approval.id) is None
    assert [a.id for a in await service.list_for_user(user_id=str(owner), status="pending")] == [
        approval.id
    ]
    with pytest.raises(ApprovalError) as mismatch:
        await service.decide(
            user_id=str(owner),
            approval_id=approval.id,
            decision="approve",
            args_hash=canonical_args_hash({"text": "other"}),
            channel="web",
        )
    assert mismatch.value.code == "approval_args_mismatch"
    with pytest.raises(ApprovalError) as foreign:
        await service.decide(
            user_id=str(stranger),
            approval_id=approval.id,
            decision="approve",
            args_hash=approval.args_hash,
            channel="web",
        )
    assert foreign.value.code == "approval_not_found"

    waiter = asyncio.create_task(
        service.wait(user_id=str(owner), approval_id=approval.id, timeout_s=5)
    )
    await asyncio.sleep(0.05)
    decided = await service.decide(
        user_id=str(owner),
        approval_id=approval.id,
        decision="approve",
        args_hash=approval.args_hash,
        channel="android",
    )
    assert decided.status == "approved" and (await asyncio.wait_for(waiter, 2)).status == "approved"
    with pytest.raises(ApprovalError) as twice:
        await service.decide(
            user_id=str(owner),
            approval_id=approval.id,
            decision="deny",
            args_hash=approval.args_hash,
            channel="web",
        )
    assert twice.value.code == "approval_already_decided"

    consume = dict(
        user_id=str(owner),
        approval_id=approval.id,
        run_id=str(run_id),
        tool=f"{PLUGIN_ID}.send",
        args_hash=approval.args_hash,
    )
    results = await asyncio.gather(
        service.consume(**consume), service.consume(**consume), return_exceptions=True
    )
    assert sum(1 for r in results if not isinstance(r, BaseException)) == 1
    assert [r.code for r in results if isinstance(r, ApprovalError)] == ["approval_already_used"]

    late = await service.request(approval_request(owner, run_id))
    clock.advance(121)
    assert (await service.get(user_id=str(owner), approval_id=late.id)) is not None
    assert (await service.get(user_id=str(owner), approval_id=late.id)).status == "expired"  # type: ignore[union-attr]
    expired = await service.list_for_user(user_id=str(owner), status="expired")
    assert [a.id for a in expired] == [late.id]
    with pytest.raises(ApprovalError) as too_late:
        await service.decide(
            user_id=str(owner),
            approval_id=late.id,
            decision="approve",
            args_hash=late.args_hash,
            channel="web",
        )
    assert too_late.value.code == "approval_expired"
    timed_out = await service.wait(user_id=str(owner), approval_id=late.id, timeout_s=0.01)
    assert timed_out.status == "expired"


async def install_row(
    factory: async_sessionmaker[AsyncSession],
    user: uuid.UUID,
    digest: str,
    body: dict[str, Any],
) -> tuple[str, str]:
    async with factory() as session, session.begin():
        workspace = await ensure_personal_workspace(session, user)
        row = Installation(
            workspace_id=workspace,
            plugin_id=PLUGIN_ID,
            version="1.0.0",
            status="active",
            source="catalog",
            grants=["state.read", "state.write"],
            tools_digest=digest,
            manifest=body,
        )
        session.add(row)
        await session.flush()
        return str(workspace), str(row.id)


async def test_sql_installations_are_tenant_scoped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner, stranger = await create_users(factory, 2)
    workspace, installation = await install_row(factory, owner, "sha256:x", manifest())
    reader = SqlInstallationReader(factory)

    [own] = await reader.installations(user_id=str(owner), workspace_id=None)
    assert (own.id, own.workspace_id, own.tools_digest) == (installation, workspace, "sha256:x")
    assert await reader.installations(user_id=str(stranger), workspace_id=workspace) == []
    assert await reader.installations(user_id=str(stranger), workspace_id=None) == []
    assert await reader.installations(user_id="not-a-uuid", workspace_id=None) == ()


async def test_sql_end_to_end_audit_and_approval_rows(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    [user] = await create_users(factory, 1)
    recorder = Recorder()
    server = build_server(recorder)
    body = manifest()
    workspace, installation = await install_row(factory, user, await digest_of(server), body)
    approvals = SqlApprovalService(factory, poll_s=0.05)
    router = McpToolRouter(
        registry=PluginRegistry(
            SqlInstallationReader(factory), StaticManifestSource([LoadedManifest(body, None)])
        ),
        connector=McpConnector(server_factory=lambda raw, plugin_dir: server),
        audit=SqlToolAudit(factory),
        approvals=approvals,
        config=RouterConfig(list_cache_s=0),
    )
    gateway = scripted(
        tool_step("call_a", f"{PLUGIN_ID}.start", {"seconds": 5}),
        tool_step("call_b", f"{PLUGIN_ID}.send", {"text": "секрет"}),
        text_step(answer("Готово.")),
    )
    service = RunService(
        RunEngine(
            store=SqlRunStore(factory),
            notifier=RunNotifier(),
            gateway=gateway,
            config=EngineConfig(limits=TEST_LIMITS),
            router=router,
            approvals=approvals,
        ),
        keepalive_s=0.2,
    )
    record, _ = await service.start(NewRun(user_id=user, input_text="x", profile="chat"))

    async def approve() -> None:
        async for event in service.events(user_id=user, run_id=record.id):
            if event is not None and event.type == "approval.requested":
                wire = event.to_wire()["payload"]["approval"]
                await approvals.decide(
                    user_id=str(user),
                    approval_id=wire["id"],
                    decision="approve",
                    args_hash=wire["args_hash"],
                    channel="web",
                )
                return

    approver = asyncio.create_task(approve())
    events = await collect(service, user, record.id)
    await asyncio.wait_for(approver, 5)
    await service.wait_finished(record.id)
    assert payload_of(events, "run.completed")["tool_calls"] == 2
    assert recorder.names() == ["start", "send"]
    assert recorder.calls[0][2]["mia/workspace_id"] == workspace

    async with factory() as session:
        rows = (
            await session.scalars(
                select(ToolExecution)
                .where(ToolExecution.run_id == record.id)
                .order_by(ToolExecution.created_at)
            )
        ).all()
        approval_rows = (
            await session.scalars(select(ToolApproval).where(ToolApproval.run_id == record.id))
        ).all()
    statuses = sorted(row.status.value for row in rows)
    assert statuses == ["approval_required", "succeeded", "succeeded"]
    for row in rows:
        assert row.operation_id is not None and row.call_id == row.operation_id
        assert row.turn_id is None and row.result is None
        assert row.installation_id == installation and row.plugin_id == PLUGIN_ID
        assert "секрет" not in repr(row.__dict__)
    executed = [row for row in rows if row.status.value == "succeeded"]
    assert all(row.result_digest and row.duration_ms is not None for row in executed)
    [approval] = approval_rows
    assert approval.status == "approved" and approval.consumed_at is not None
    assert approval.arguments == {"text": "секрет"}


# -- HTTP API -----------------------------------------------------------------------


async def test_approvals_api_owner_only_with_bearer_and_cookie() -> None:
    async with build_harness() as harness:
        settings = Settings(app_env="test")
        database = Database(database_url())
        runtime = Runtime(database=database, redis=RedisClient(settings.redis_url))
        stack = install_tools(harness.app, settings, runtime)
        try:
            async with harness.client() as android, harness.client() as browser:
                auth = await android_login(android, "sub-approvals")
                owner = (await android.get("/v1/workspace", headers=auth)).json()["owner_user_id"]
                approval = await stack.approvals.request(
                    approval_request(uuid.UUID(owner), uuid.uuid4(), "Надо купить молоко")
                )
                listed = await android.get("/v1/approvals", headers=auth)
                assert listed.status_code == 200
                assert [item["id"] for item in listed.json()["items"]] == [approval.id]
                got = await android.get(f"/v1/approvals/{approval.id}", headers=auth)
                assert got.json()["args_hash"] == approval.args_hash
                assert got.json()["decision"] is None

                await web_login(browser, "sub-stranger")
                foreign = await browser.get(f"/v1/approvals/{approval.id}")
                assert foreign.status_code == 404
                assert foreign.json()["error"]["code"] == "approval_not_found"
                denied = await browser.post(
                    f"/v1/approvals/{approval.id}/decision",
                    headers=web_headers(),
                    json={"decision": "approve", "args_hash": approval.args_hash},
                )
                assert denied.status_code == 404

                wrong = await android.post(
                    f"/v1/approvals/{approval.id}/decision",
                    headers={**auth, **key()},
                    json={"decision": "approve", "args_hash": "sha256:" + "0" * 64},
                )
                assert (wrong.status_code, wrong.json()["error"]["code"]) == (
                    409,
                    "approval_args_mismatch",
                )
                missing_key = await android.post(
                    f"/v1/approvals/{approval.id}/decision",
                    headers=auth,
                    json={"decision": "approve", "args_hash": approval.args_hash},
                )
                assert missing_key.status_code == 422
                ok = await android.post(
                    f"/v1/approvals/{approval.id}/decision",
                    headers={**auth, **key()},
                    json={"decision": "approve", "args_hash": approval.args_hash},
                )
                assert ok.status_code == 200
                assert ok.json()["status"] == "approved"
                assert ok.json()["decision"]["channel"] == "android"
                replay = await android.post(
                    f"/v1/approvals/{approval.id}/decision",
                    headers={**auth, **key()},
                    json={"decision": "approve", "args_hash": approval.args_hash},
                )
                assert replay.status_code == 200
                flip = await android.post(
                    f"/v1/approvals/{approval.id}/decision",
                    headers={**auth, **key()},
                    json={"decision": "deny", "args_hash": approval.args_hash},
                )
                assert (flip.status_code, flip.json()["error"]["code"]) == (
                    409,
                    "approval_already_decided",
                )

                # Web cookie: same-origin decision is recorded with channel «web»;
                # a cross-site cookie mutation is refused.
                web_owner = await web_login(browser, "sub-approvals")
                assert web_owner["user"]["id"] == owner
                second = await stack.approvals.request(
                    approval_request(uuid.UUID(owner), uuid.uuid4())
                )
                cross = await browser.post(
                    f"/v1/approvals/{second.id}/decision",
                    headers={
                        **key(),
                        "Origin": "https://evil.example",
                        "Sec-Fetch-Site": "cross-site",
                    },
                    json={"decision": "approve", "args_hash": second.args_hash},
                )
                assert cross.status_code == 403
                web = await browser.post(
                    f"/v1/approvals/{second.id}/decision",
                    headers=web_headers(),
                    json={"decision": "deny", "args_hash": second.args_hash},
                )
                assert web.status_code == 200 and web.json()["decision"]["channel"] == "web"
                anonymous = await android.get("/v1/approvals")
                assert anonymous.status_code == 401
        finally:
            await database.close()
            await runtime.redis.close()


async def test_expired_decision_is_conflict(factory: async_sessionmaker[AsyncSession]) -> None:
    [user] = await create_users(factory, 1)
    clock = Clock()
    service = SqlApprovalService(factory, clock=clock)
    approval = await service.request(approval_request(user, uuid.uuid4()))
    clock.now += timedelta(seconds=500)
    with pytest.raises(ApprovalError) as error:
        await service.decide(
            user_id=str(user),
            approval_id=approval.id,
            decision="approve",
            args_hash=approval.args_hash,
            channel="web",
        )
    assert error.value.code == "approval_expired"
