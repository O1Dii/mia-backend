"""Port semantics, exercised through the fakes that later sessions reuse."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.ports import (
    Approval,
    ApprovalError,
    ApprovalRequest,
    ApprovalService,
    DeviceToolCall,
    DeviceToolChannel,
    DeviceToolDeclaration,
    DeviceToolResult,
    FactRecord,
    MemoryProvider,
    MemorySource,
    ModelGateway,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PersonRef,
    TextDelta,
    ToolCallRequest,
    ToolCallRequested,
    ToolContext,
    ToolDescriptor,
    ToolOutcome,
    ToolRouter,
    ToolSpec,
    canonical_args_hash,
)
from tests.fakes.contracts import assert_mvp_valid
from tests.fakes import (
    FakeApprovalService,
    FakeDeviceToolChannel,
    FakeMemoryProvider,
    FakeModelGateway,
    FakePerson,
    FakeToolRouter,
    text_step,
    tool_step,
)

USER = "user-1"
DEVICE = "a1b2c3d4-e5f6-4789-8abc-def012345678"
RUN = "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def test_fakes_satisfy_ports() -> None:
    approvals = FakeApprovalService()
    devices = FakeDeviceToolChannel()
    gateway: ModelGateway = FakeModelGateway()
    router: ToolRouter = FakeToolRouter(approvals, devices=devices)
    memory: MemoryProvider = FakeMemoryProvider()
    channel: DeviceToolChannel = devices
    service: ApprovalService = approvals

    for port, protocol in (
        (gateway, ModelGateway),
        (router, ToolRouter),
        (memory, MemoryProvider),
        (channel, DeviceToolChannel),
        (service, ApprovalService),
    ):
        assert isinstance(port, protocol)


# --- ModelGateway ------------------------------------------------------------


async def test_model_gateway_replays_script_and_records_requests() -> None:
    gateway = FakeModelGateway(
        [tool_step("c1", "builtin.timer.start", {"seconds": 600}), text_step("Готово", ".")]
    )
    request = ModelRequest(
        user_id=USER, profile="voice", messages=[ModelMessage(role="user", content="Таймер")]
    )

    first = [event async for event in gateway.stream(request)]
    second = [event async for event in gateway.stream(request)]

    assert isinstance(first[0], ToolCallRequested)
    assert first[0].call.arguments == {"seconds": 600}
    assert [e.text for e in second if isinstance(e, TextDelta)] == ["Готово", "."]
    assert second[-1].kind == "done" and len(gateway.requests) == 2
    assert (await gateway.describe(user_id=USER)).verified


async def test_model_gateway_errors_are_port_errors() -> None:
    gateway = FakeModelGateway([ModelGatewayError("provider_timeout", "slow", retryable=True)])
    request = ModelRequest(user_id=USER, profile="chat", messages=[])

    with pytest.raises(ModelGatewayError) as error:
        _ = [event async for event in gateway.stream(request)]

    assert error.value.code == "provider_timeout" and error.value.retryable
    with pytest.raises(ModelGatewayError, match="exhausted"):
        _ = [event async for event in gateway.stream(request)]


# --- ApprovalService ---------------------------------------------------------


def approval_request(arguments: dict[str, Any]) -> ApprovalRequest:
    return ApprovalRequest(
        user_id=USER,
        run_id=RUN,
        tool_call_id="call_2",
        tool="team.telegram.send_message",
        risk="write_external",
        summary="Отправить Яне Петровой в Telegram: «Надо купить молоко»",
        arguments=arguments,
        plugin_id="team.telegram",
    )


def approval_json(approval: Approval) -> dict[str, Any]:
    def timestamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    decision = None
    if approval.decision is not None:
        decision = {
            "decision": approval.decision.decision,
            "decided_at": timestamp(approval.decision.decided_at),
            "channel": approval.decision.channel,
        }
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "tool_call_id": approval.tool_call_id,
        "plugin_id": approval.plugin_id,
        "tool": approval.tool,
        "risk": approval.risk,
        "summary": approval.summary,
        "arguments": dict(approval.arguments),
        "args_hash": approval.args_hash,
        "status": approval.status,
        "created_at": timestamp(approval.created_at),
        "expires_at": timestamp(approval.expires_at),
        "decision": decision,
    }


async def test_approval_decision_is_bound_to_args_hash() -> None:
    service = FakeApprovalService(now=Clock())
    arguments = {"to": "person:42", "text": "Надо купить молоко"}
    approval = await service.request(approval_request(arguments))

    assert approval.args_hash == canonical_args_hash(arguments)
    assert_mvp_valid(approval_json(approval), "approval")
    with pytest.raises(ApprovalError) as mismatch:
        await service.decide(
            user_id=USER,
            approval_id=approval.id,
            decision="approve",
            args_hash=canonical_args_hash({**arguments, "text": "Другое"}),
            channel="android",
        )
    assert mismatch.value.code == "approval_args_mismatch"

    decided = await service.decide(
        user_id=USER,
        approval_id=approval.id,
        decision="approve",
        args_hash=approval.args_hash,
        channel="android",
    )
    assert decided.status == "approved"
    assert_mvp_valid(approval_json(decided), "approval")
    with pytest.raises(ApprovalError, match="already decided"):
        await service.decide(
            user_id=USER,
            approval_id=approval.id,
            decision="deny",
            args_hash=approval.args_hash,
            channel="web",
        )
    assert await service.get(user_id="someone-else", approval_id=approval.id) is None


async def test_approval_expires_and_wait_returns_decision() -> None:
    clock = Clock()
    service = FakeApprovalService(now=clock)
    expiring = await service.request(approval_request({"a": 1}))
    waiting = await service.request(approval_request({"b": 2}))

    clock.value += timedelta(seconds=121)
    expired = await service.get(user_id=USER, approval_id=expiring.id)
    assert expired is not None and expired.status == "expired"
    with pytest.raises(ApprovalError) as error:
        await service.decide(
            user_id=USER,
            approval_id=expiring.id,
            decision="approve",
            args_hash=expiring.args_hash,
            channel="web",
        )
    assert error.value.code == "approval_expired"

    clock.value = waiting.created_at
    waiter = asyncio.create_task(service.wait(user_id=USER, approval_id=waiting.id, timeout_s=1))
    await asyncio.sleep(0)
    await service.decide(
        user_id=USER,
        approval_id=waiting.id,
        decision="deny",
        args_hash=waiting.args_hash,
        channel="web",
    )
    assert (await waiter).status == "denied"


# --- ToolRouter --------------------------------------------------------------


def descriptor(
    name: str,
    *,
    risk: str = "read",
    voice: bool = False,
    plugin_id: str | None = "team.linear",
    executor: str = "server",
) -> ToolDescriptor:
    return ToolDescriptor(
        spec=ToolSpec(
            name=name,
            description="Отправить сообщение",
            input_schema={"type": "object", "required": ["text"]},
        ),
        risk=risk,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        voice=voice,
        plugin_id=plugin_id,
    )


async def echo(context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
    return ToolOutcome(
        call_id=request.call_id,
        status="succeeded",
        content=f"ok {request.arguments['text']}",
        structured_content={"echo": dict(request.arguments)},
    )


def context(profile: str = "chat", **kwargs: Any) -> ToolContext:
    return ToolContext(user_id=USER, run_id=RUN, profile=profile, **kwargs)  # type: ignore[arg-type]


async def test_router_filters_tools_by_profile_and_scopes() -> None:
    router = FakeToolRouter(FakeApprovalService())
    router.register(descriptor("team.linear.list_issues", voice=True), echo)
    router.register(descriptor("team.linear.create_issue", risk="write_reversible"), echo)
    router.register(descriptor("team.mail.send", plugin_id="team.mail"), echo)

    voice = [t.spec.name for t in await router.list_tools(context("voice"))]
    app = [
        t.spec.name
        for t in await router.list_tools(context("app", scopes=frozenset({"tools.use:team.mail"})))
    ]

    assert voice == ["team.linear.list_issues"]
    assert app == ["team.mail.send"]
    assert len(await router.list_tools(context("chat"))) == 3
    hidden = await router.call(
        context("voice"), ToolCallRequest("c1", "team.linear.create_issue", {"text": "x"})
    )
    assert hidden.status == "failed" and hidden.error_code == "tool_not_found"
    invalid = await router.call(context(), ToolCallRequest("c2", "team.linear.list_issues", {}))
    assert invalid.error_code == "tool_invalid_arguments"


async def test_write_external_requires_matching_single_use_approval() -> None:
    approvals = FakeApprovalService()
    router = FakeToolRouter(approvals)
    router.register(
        descriptor("team.telegram.send_message", risk="write_external", plugin_id="team.telegram"),
        echo,
    )
    request = ToolCallRequest("c1", "team.telegram.send_message", {"text": "молоко"})

    gated = await router.call(context(), request)
    assert gated.status == "approval_required" and gated.approval is not None
    assert router.executed == []

    approval = gated.approval
    await approvals.decide(
        user_id=USER,
        approval_id=approval.id,
        decision="approve",
        args_hash=approval.args_hash,
        channel="android",
    )
    changed = ToolCallRequest(
        "c1", "team.telegram.send_message", {"text": "хлеб"}, approval_id=approval.id
    )
    assert (await router.call(context(), changed)).status == "denied"

    approved = ToolCallRequest(
        "c1", "team.telegram.send_message", {"text": "молоко"}, approval_id=approval.id
    )
    done = await router.call(context(), approved)
    assert done.status == "succeeded" and done.content == "ok молоко"
    assert (await router.call(context(), approved)).status == "denied"
    assert len(router.executed) == 1


# --- DeviceToolChannel -------------------------------------------------------


def declaration(name: str, risk: str = "read") -> DeviceToolDeclaration:
    return DeviceToolDeclaration(
        name=name,
        description="Device tool",
        input_schema={"type": "object"},
        risk=risk,  # type: ignore[arg-type]
        voice=True,
        requires_unlocked=False,
        timeout_ms=3000,
    )


def device_call(call_id: str, tool: str, seconds: float) -> DeviceToolCall:
    return DeviceToolCall(
        call_id=call_id,
        run_id=RUN,
        device_id=DEVICE,
        tool=tool,
        arguments={"query": "Яна"},
        deadline_at=datetime.now(UTC) + timedelta(seconds=seconds),
    )


async def test_device_call_round_trip_and_late_results() -> None:
    channel = FakeDeviceToolChannel()
    await channel.register(
        user_id=USER, device_id=DEVICE, tools=[declaration("device.contacts.search")]
    )

    task = asyncio.create_task(
        channel.invoke(user_id=USER, call=device_call("c1", "device.contacts.search", 5))
    )
    await asyncio.sleep(0)
    result = DeviceToolResult(call_id="c1", status="succeeded", content="2 контакта")
    assert not await channel.deliver_result(user_id="intruder", device_id=DEVICE, result=result)
    assert await channel.deliver_result(user_id=USER, device_id=DEVICE, result=result)
    assert (await task).content == "2 контакта"
    assert not await channel.deliver_result(user_id=USER, device_id=DEVICE, result=result)
    assert [call.call_id for call in channel.sent] == ["c1"]

    unsupported = await channel.invoke(user_id=USER, call=device_call("c2", "device.sms.send", 5))
    assert unsupported.status == "failed" and unsupported.error_code == "unsupported_tool"


async def test_device_timeout_is_uncertain_for_writes() -> None:
    channel = FakeDeviceToolChannel()
    await channel.register(
        user_id=USER,
        device_id=DEVICE,
        tools=[
            declaration("device.contacts.search"),
            declaration("telegram.share", "write_reversible"),
        ],
    )

    read = await channel.invoke(user_id=USER, call=device_call("c1", "device.contacts.search", 0))
    write = await channel.invoke(user_id=USER, call=device_call("c2", "telegram.share", 0))

    assert (read.status, read.error_code) == ("failed", "timeout")
    assert (write.status, write.error_code) == ("uncertain", "timeout")


async def test_router_dispatches_device_tools_to_the_channel() -> None:
    channel = FakeDeviceToolChannel()
    await channel.register(user_id=USER, device_id=DEVICE, tools=[declaration("telegram.share")])
    router = FakeToolRouter(FakeApprovalService(), devices=channel)
    router.register(descriptor("telegram.share", executor="device", plugin_id=None))

    task = asyncio.create_task(
        router.call(
            context(device_id=DEVICE), ToolCallRequest("c9", "telegram.share", {"text": "x"})
        )
    )
    for _ in range(20):  # let the router reach the channel
        if channel.sent:
            break
        await asyncio.sleep(0)
    assert channel.sent
    await channel.deliver_result(
        user_id=USER,
        device_id=DEVICE,
        result=DeviceToolResult(call_id="c9", status="succeeded", content="opened"),
    )

    assert (await task).status == "succeeded"


# --- MemoryProvider ----------------------------------------------------------


async def test_memory_suggestions_need_consent_and_context_uses_active_facts() -> None:
    memory = FakeMemoryProvider(auto_save_categories={"interests"})
    source = MemorySource(type="turn", ref="turn-1")

    suggested = await memory.suggest(
        user_id=USER, text="Работает в Импел", category="work", source=source
    )
    auto = await memory.suggest(
        user_id=USER, text="Любит джаз", category="interests", source=source
    )
    memory.add_fact(
        USER,
        FactRecord(
            id="f-active",
            text="Работает удалённо по пятницам",
            category="work",
            status="active",
            sensitivity="normal",
            source=source,
        ),
    )

    assert (suggested.status, auto.status) == ("suggested", "active")
    found = await memory.context(user_id=USER, query="Где он работает по пятницам?")
    assert [fact.id for fact in found] == ["f-active"]
    assert await memory.context(user_id="other", query="работает") == []


async def test_people_resolve_by_alias_without_identifiers() -> None:
    memory = FakeMemoryProvider()
    yana = PersonRef(id="42", display_name="Яна Петрова", relation="жена")
    memory.add_person(
        USER, FakePerson(ref=yana, aliases=("Яна", "жена"), identifiers={"telegram_user_id": 7})
    )

    matches = await memory.resolve_person(user_id=USER, name="яна")

    assert matches == [yana] and matches[0].model_ref == "person:42"
    assert await memory.person_identifiers(user_id=USER, person_id="42") == {"telegram_user_id": 7}
    assert await memory.person_identifiers(user_id="other", person_id="42") == {}
