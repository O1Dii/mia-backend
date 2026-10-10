"""``FeedItem.steps`` (M74-R1): after a reload, and on another device, the history still shows
which tools an answer called, why one failed and the approval decided on the phone."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest
from app.api.v1.approvals import build_approvals_router
from app.core.ports.approval_service import Approval, ApprovalRequest
from app.core.ports.model_gateway import ModelDone, ModelToolCall, ToolCallRequested
from app.core.ports.tool_router import ToolCallRequest, ToolContext, ToolOutcome
from app.modules.auth.composition import request_authenticator
from app.modules.conversations.composition import ChatsConfig, install_chats
from app.modules.conversations.feed import FeedService
from app.modules.conversations.steps import STEP_LIMIT
from app.modules.runs import (
    EngineConfig,
    NewRun,
    ProfileLimits,
    RunEngine,
    RunNotifier,
    RunService,
    SqlRunStore,
)
from app.modules.tools.approvals import SqlApprovalService
from app.modules.tools.models import ToolApproval
from sqlalchemy import select

from tests.chats.conftest import _Runtime, check
from tests.fakes import FakeModelGateway, FakeToolRouter, text_step, tool_step
from tests.runs.conftest import TEST_LIMITS, collect
from tests.runs.gateways import answer
from tests.runs.tools import descriptor, echo_handler
from tests.workspace.harness import (
    Harness,
    android_login,
    build_harness,
    key,
    requires_postgres,
    web_login,
)

pytestmark = requires_postgres
LIMITS = replace(
    TEST_LIMITS,
    chat=ProfileLimits(max_steps=6, max_tool_calls=STEP_LIMIT + 8, timeout_s=10),
)
SUBJECT = "steps-owner"
PHONE = uuid.uuid4()


async def not_found(context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
    return ToolOutcome(
        call_id=request.call_id,
        status="failed",
        content="Ничего не найдено по запросу «электромобили 2026»",
        error_code="search_empty",
    )


class PhoneApprovals(SqlApprovalService):
    """Every approval may be decided only on ``PHONE`` (an M70 device consent)."""

    async def request(self, request: ApprovalRequest) -> Approval:
        consent = {
            "waiting_for": "device",
            "target_device_id": str(PHONE),
            "target_device_name": "Pixel",
            "requester": {"kind": "web", "label": "Браузер"},
            "capability": "telegram.draft",
            "capability_kind": "external",
            "capability_label": "Отправить сообщение",
            "model_sees": "Только результат отправки",
            "scopes": ["run"],
        }
        return await super().request(replace(request, device_consent=consent))


@dataclass
class _DeviceStore:
    async def session_device(self, *, user_id: str, family_id: str) -> str:
        return str(PHONE)


@dataclass
class _DeviceAccess:
    """The phone that holds the bearer session is the consent's target device (M70 gate)."""

    store: _DeviceStore = field(default_factory=_DeviceStore)
    decided_calls: list[str] = field(default_factory=list)

    async def decided(self, *, user_id: str, approval: Approval, decision: str, scope: str) -> None:
        self.decided_calls.append(approval.id)


@dataclass
class StepsWorld:
    harness: Harness
    runs: RunService
    approvals: PhoneApprovals


@pytest.fixture
async def steps_world() -> AsyncIterator[StepsWorld]:
    async with build_harness() as harness:
        approvals = PhoneApprovals(harness.factory, poll_s=0.05)
        router = FakeToolRouter(approvals)
        router.register(descriptor("search.find"), not_found)
        router.register(
            descriptor("mail.send", risk="write_external", plugin_id="gate.mailer"), echo_handler
        )
        for index in range(STEP_LIMIT + 3):
            router.register(descriptor(f"bulk.t{index}"), echo_handler)
        gateway = FakeModelGateway([])
        engine = RunEngine(
            store=SqlRunStore(harness.factory),
            notifier=RunNotifier(),
            gateway=gateway,
            config=EngineConfig(limits=LIMITS),
            router=router,
            approvals=approvals,
            feed=FeedService(harness.factory),
        )
        runs = RunService(engine, keepalive_s=0.2)
        install_chats(
            harness.app,
            _Runtime(harness.factory),  # type: ignore[arg-type]
            runs=runs,
            config=ChatsConfig(retention_sweep_s=0),
        )
        harness.app.include_router(
            build_approvals_router(
                service=approvals,
                authenticator=request_authenticator(harness.app),
                device_access=cast(Any, _DeviceAccess()),
            )
        )
        yield StepsWorld(harness=harness, runs=runs, approvals=approvals)


async def _pending(world: StepsWorld) -> ToolApproval:
    for _ in range(200):
        async with world.harness.factory() as session:
            row = await session.scalar(select(ToolApproval).where(ToolApproval.status == "pending"))
        if row is not None:
            return row
        await asyncio.sleep(0.02)
    raise AssertionError("no approval was requested")


async def test_feed_keeps_steps_failure_reason_and_phone_approval(steps_world: StepsWorld) -> None:
    world = steps_world
    gateway = world.runs.engine.gateway
    assert isinstance(gateway, FakeModelGateway)
    gateway.add_step(tool_step("c1", "search.find", {"q": "электромобили 2026"}))
    gateway.add_step(tool_step("c2", "mail.send", {"q": "Отчёт Яне"}))
    gateway.add_step(text_step(answer("Ничего не нашла, но письмо отправила.")))
    async with world.harness.client() as phone, world.harness.client() as browser:
        bearer = await android_login(phone, SUBJECT)
        user = uuid.UUID((await phone.get("/v1/me", headers=bearer)).json()["id"])
        record, _ = await world.runs.start(
            NewRun(user_id=user, profile="chat", input_text="Найди и отправь отчёт")
        )
        pending = await _pending(world)
        decided = await phone.post(
            f"/v1/approvals/{pending.id}/decision",
            json={"decision": "approve", "args_hash": pending.args_hash},
            headers={**bearer, **key()},
        )
        assert decided.status_code == 200, decided.text
        events = await collect(world.runs, user, record.id)
        assert events[-1]["type"] == "run.completed"

        # Another device, a fresh page load: the web reads the chat with its cookie.
        await web_login(browser, SUBJECT)
        page = (await browser.get("/v1/feed")).json()
        check("FeedPage", page)
        user_item, answer_item = page["items"][1], page["items"][0]
        assert "steps" not in user_item
        search, mail = answer_item["steps"]
        assert search == {
            "tool_call_id": "c1",
            "tool": "search.find",
            "risk": "read",
            "executor": "server",
            "status": "failed",
            "arguments": {"q": "электромобили 2026"},
            "error_code": "search_empty",
            "error_detail": "Ничего не найдено по запросу «электромобили 2026»",
        }
        approval = mail.pop("approval")
        assert mail == {
            "tool_call_id": "c2",
            "tool": "mail.send",
            "plugin_id": "gate.mailer",
            "risk": "write_external",
            "executor": "server",
            "status": "succeeded",
            "arguments": {"q": "Отчёт Яне"},
        }
        assert approval["id"] == str(pending.id) and approval["status"] == "approved"
        assert (approval["channel"], approval["device_id"]) == ("android", str(PHONE))
        assert approval["summary"] and approval["decided_at"].endswith("Z")


async def test_steps_are_capped_and_absent_without_tools(steps_world: StepsWorld) -> None:
    world = steps_world
    gateway = world.runs.engine.gateway
    assert isinstance(gateway, FakeModelGateway)
    gateway.add_step(
        [
            *(
                ToolCallRequested(ModelToolCall(f"b{i}", f"bulk.t{i}", {"q": str(i)}))
                for i in range(STEP_LIMIT + 3)
            ),
            ModelDone("tool_calls"),
        ]
    )
    gateway.add_step(text_step(answer("Готово.")))
    gateway.add_step(text_step(answer("Привет.")))
    async with world.harness.client() as phone:
        bearer = await android_login(phone, SUBJECT)
        user = uuid.UUID((await phone.get("/v1/me", headers=bearer)).json()["id"])
        for text_value in ("Много шагов", "Привет"):
            record, _ = await world.runs.start(
                NewRun(user_id=user, profile="chat", input_text=text_value)
            )
            await collect(world.runs, user, record.id)
        page = (await phone.get("/v1/feed", headers=bearer)).json()
        check("FeedPage", page)
        plain, _, many, _ = page["items"]
        assert "steps" not in plain
        steps = many["steps"]
        assert len(steps) == STEP_LIMIT
        assert [step["tool_call_id"] for step in steps] == [f"b{i}" for i in range(STEP_LIMIT)]
        assert {step["status"] for step in steps} == {"succeeded"}
