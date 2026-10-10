"""Run engine against fake ports (ADR-0011): profiles, tools, approvals, cancel, failures."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from app.core.ports.memory_provider import FactRecord, MemorySource
from app.core.ports.model_gateway import ModelGatewayError, ModelToolCall, ToolCallRequested
from app.core.ports.tool_router import ToolCallRequest, ToolContext, ToolOutcome
from app.modules.runs import NewRun, RunFinishedError, RunService
from app.modules.runs.defaults import UnconfiguredModelGateway
from app.modules.runs.profiles import ProfileLimits, RunLimits

from tests.fakes import FakeMemoryProvider, text_step, tool_step
from tests.runs.conftest import (
    TEST_LIMITS,
    RunWorld,
    assert_well_formed,
    collect,
    payload_of,
    types,
)
from tests.runs.gateways import (
    PLAIN,
    BlockingGateway,
    DelayedGateway,
    answer,
    scripted,
)
from tests.runs.tools import descriptor, failing_handler, router_with


async def run_to_end(
    service: RunService, user: uuid.UUID, text: str = "Привет", **fields: Any
) -> tuple[uuid.UUID, list[dict[str, Any]]]:
    record, _ = await service.start(NewRun(user_id=user, input_text=text, **fields))
    events = await collect(service, user, record.id)
    await service.wait_finished(record.id)
    assert_well_formed(events)
    return record.id, events


async def test_simple_run_emits_contract_events(world: RunWorld) -> None:
    gateway = scripted(text_step(answer("Привет! Чем помочь?")))
    service = world.service(gateway)

    run_id, events = await run_to_end(service, world.user, profile="chat")

    assert types(events) == ["run.started", "response.final", "run.completed"]
    assert payload_of(events, "run.started") == {"profile": "chat", "model": "fake/structured-1"}
    final = payload_of(events, "response.final")["response"]
    assert final == {"speech": "Привет! Чем помочь?", "display": None, "followups": []}
    assert payload_of(events, "run.completed") == {"steps": 1, "tool_calls": 0}
    record = await service.get(user_id=world.user, run_id=run_id)
    assert record.status == "completed" and record.response == final and record.steps == 1
    assert gateway.requests[0].messages[0].role == "system"
    assert gateway.requests[0].messages[-1].content == "Привет"
    assert gateway.requests[0].structured_response is True


async def test_voice_profile_cuts_steps_and_tool_calls(world: RunWorld) -> None:
    router, _ = router_with(descriptor("test.search"))
    gateway = scripted(
        tool_step("c1", "test.search", {"q": "1"}),
        tool_step("c2", "test.search", {"q": "2"}),
        text_step(answer("Нашла два результата.")),
    )
    service = world.service(gateway, router=router)

    _, events = await run_to_end(service, world.user, profile="voice")

    assert len(gateway.requests) == 3
    assert [bool(request.tools) for request in gateway.requests] == [True, True, False]
    assert [call.call_id for call in router.executed] == ["c1", "c2"]
    assert payload_of(events, "run.completed") == {"steps": 3, "tool_calls": 2}
    assert types(events).count("tool.call.started") == 2


async def test_voice_last_step_ignores_further_tool_requests(world: RunWorld) -> None:
    router, _ = router_with(descriptor("test.search"))
    gateway = scripted(
        tool_step("c1", "test.search", {"q": "1"}),
        tool_step("c2", "test.search", {"q": "2"}),
        tool_step("c3", "test.search", {"q": "3"}),
    )
    service = world.service(gateway, router=router)

    _, events = await run_to_end(service, world.user, profile="voice")

    assert len(gateway.requests) == 3 and not gateway.requests[2].tools
    assert [call.call_id for call in router.executed] == ["c1", "c2"]
    speech = payload_of(events, "response.final")["response"]["speech"]
    assert speech  # fallback speech, never empty


async def test_voice_tool_budget_skips_extra_calls_in_one_step(world: RunWorld) -> None:
    router, _ = router_with(descriptor("test.search"))
    calls = [
        ToolCallRequested(ModelToolCall(call_id=f"c{i}", name="test.search", arguments={"q": "x"}))
        for i in range(3)
    ]
    gateway = scripted([*calls, *text_step("")[-1:]], text_step(answer("Готово.")))
    service = world.service(gateway, router=router)

    _, events = await run_to_end(service, world.user, profile="voice")

    assert [call.call_id for call in router.executed] == ["c0", "c1"]
    skipped = [m for m in gateway.requests[1].messages if m.tool_call_id == "c2"]
    assert json.loads(skipped[0].content) == {"status": "skipped", "error": "tool_budget_exceeded"}
    assert not gateway.requests[1].tools
    assert payload_of(events, "run.completed")["tool_calls"] == 2


async def test_chat_profile_allows_longer_tool_loops(world: RunWorld) -> None:
    router, _ = router_with(descriptor("test.search", voice=False))
    steps = [tool_step(f"c{i}", "test.search", {"q": str(i)}) for i in range(5)]
    gateway = scripted(*steps, text_step(answer("Готово.")))
    service = world.service(gateway, router=router)

    _, events = await run_to_end(service, world.user, profile="chat")

    assert len(router.executed) == 5
    assert payload_of(events, "run.completed") == {"steps": 6, "tool_calls": 5}


async def test_voice_hides_non_voice_tools(world: RunWorld) -> None:
    router, _ = router_with(descriptor("test.voice_ok"), descriptor("test.screen", voice=False))
    gateway = scripted(text_step(answer("Ок.")))
    service = world.service(gateway, router=router)

    await run_to_end(service, world.user, profile="voice")

    assert [spec.name for spec in gateway.requests[0].tools] == ["test.voice_ok"]


async def test_tool_events_and_model_sees_only_compact_content(world: RunWorld) -> None:
    router, _ = router_with(descriptor("test.search", plugin_id="test.tools"))
    gateway = scripted(tool_step("c1", "test.search", {"q": "кофе"}), text_step(answer("Вот.")))
    service = world.service(gateway, router=router)

    _, events = await run_to_end(service, world.user, profile="chat")

    assert payload_of(events, "tool.call.started") == {
        "tool_call_id": "c1",
        "tool": "test.search",
        "risk": "read",
        "executor": "server",
        "plugin_id": "test.tools",
        "arguments": {"q": "кофе"},
    }
    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "succeeded"
    assert completed["structured_content"] == {"items": [1, 2, 3], "secret_for_ui_only": True}
    assert completed["ui_resource_uri"] == "ui://test.tools/list"
    tool_message = gateway.requests[1].messages[-1]
    assert tool_message.role == "tool" and tool_message.tool_call_id == "c1"
    assert json.loads(tool_message.content) == {"status": "succeeded", "content": "result for кофе"}
    assistant = gateway.requests[1].messages[-2]
    assert assistant.role == "assistant" and assistant.tool_calls[0].call_id == "c1"


async def test_unknown_tool_and_duplicate_call_ids(world: RunWorld) -> None:
    router, _ = router_with(descriptor("test.search"))
    gateway = scripted(
        tool_step("c1", "test.missing", {}),
        tool_step("c2", "test.search", {"q": "a"}),
        tool_step("c2", "test.search", {"q": "a"}),
        tool_step("c2", "test.search", {"q": "b"}),
        text_step(answer("Ок.")),
    )
    service = world.service(gateway, router=router)

    _, events = await run_to_end(service, world.user, profile="chat")

    assert [call.call_id for call in router.executed] == ["c2"]
    contents = [json.loads(r.messages[-1].content) for r in gateway.requests[1:]]
    assert contents[0] == {"status": "failed", "error": "tool_not_found"}
    assert contents[1] == contents[2] == {"status": "succeeded", "content": "result for a"}
    assert contents[3] == {"status": "failed", "error": "duplicate_call_id"}
    assert types(events).count("tool.call.started") == 1


async def test_failing_tool_is_reported_without_details(world: RunWorld) -> None:
    router, _ = router_with()
    router.register(descriptor("test.broken"), failing_handler)
    gateway = scripted(tool_step("c1", "test.broken", {}), text_step(answer("Не вышло.")))
    service = world.service(gateway, router=router)

    _, events = await run_to_end(service, world.user, profile="chat")

    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "failed"
    assert completed["error"]["code"] == "tool_execution_failed"
    assert "internals" not in json.dumps(events, ensure_ascii=False)


async def decide_when_requested(
    service: RunService, user: uuid.UUID, run_id: uuid.UUID, approvals: Any, decision: str
) -> None:
    async for event in service.events(user_id=user, run_id=run_id):
        if event is not None and event.type == "approval.requested":
            approval = event.payload["approval"]
            await approvals.decide(
                user_id=str(user),
                approval_id=approval["id"],
                decision=decision,
                args_hash=approval["args_hash"],
                channel="android",
            )
            return


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_external_write_waits_for_approval(world: RunWorld, decision: str) -> None:
    router, approvals = router_with(descriptor("test.send", risk="write_external"))
    gateway = scripted(tool_step("c1", "test.send", {"q": "привет"}), text_step(answer("Ок.")))
    service = world.service(gateway, router=router, approvals=approvals)

    record, _ = await service.start(
        NewRun(user_id=world.user, profile="chat", input_text="Отправь")
    )
    decider = asyncio.create_task(
        decide_when_requested(service, world.user, record.id, approvals, decision)
    )
    events = await collect(service, world.user, record.id)
    await decider
    assert_well_formed(events)

    assert types(events)[1:5] == [
        "tool.call.started",
        "approval.requested",
        "approval.resolved",
        "tool.call.completed",
    ]
    requested = payload_of(events, "approval.requested")["approval"]
    assert requested["status"] == "pending" and requested["arguments"] == {"q": "привет"}
    resolved = payload_of(events, "approval.resolved")
    completed = payload_of(events, "tool.call.completed")
    if decision == "approve":
        assert resolved["status"] == "approved"
        assert completed["status"] == "succeeded"
        assert [call.approval_id for call in router.executed] == [requested["id"]]
    else:
        assert resolved["status"] == "denied"
        assert completed["status"] == "denied"
        assert completed["error"]["code"] == "approval_denied"
        assert router.executed == []


async def test_no_speech_ack_while_waiting_for_approval(world: RunWorld) -> None:
    """OI-M22-002: the client speaks the approval prompt; a later filler would talk over it."""
    limits = RunLimits(
        voice=ProfileLimits(max_steps=3, max_tool_calls=2, timeout_s=5, ack_delay_ms=50),
        chat=TEST_LIMITS.chat,
        job=TEST_LIMITS.job,
        app=TEST_LIMITS.app,
    )
    router, approvals = router_with(descriptor("test.send", risk="write_external"))
    gateway = scripted(tool_step("c1", "test.send", {"q": "привет"}), text_step(answer("Ок.")))
    service = world.service(gateway, router=router, approvals=approvals, limits=limits)

    record, _ = await service.start(
        NewRun(user_id=world.user, profile="voice", input_text="Отправь")
    )

    async def decide_late() -> None:
        await asyncio.sleep(0.2)
        await decide_when_requested(service, world.user, record.id, approvals, "approve")

    decider = asyncio.create_task(decide_late())
    events = await collect(service, world.user, record.id)
    await decider
    kinds = types(events)
    assert "approval.requested" in kinds
    # A filler before the prompt is fine (slow first step under load); never after it.
    assert "speech.ack" not in kinds[kinds.index("approval.requested") :]


async def test_approval_without_service_is_denied(world: RunWorld) -> None:
    router, _ = router_with(descriptor("test.send", risk="write_external"))
    gateway = scripted(tool_step("c1", "test.send", {"q": "x"}), text_step(answer("Ок.")))
    service = world.service(gateway, router=router, approvals=None)

    _, events = await run_to_end(service, world.user, profile="chat")

    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "denied"
    assert completed["error"]["code"] == "approval_unavailable"


async def test_cancel_during_model_stream(world: RunWorld) -> None:
    gateway = BlockingGateway(answer("Никогда."))
    service = world.service(gateway)
    record, _ = await service.start(NewRun(user_id=world.user, profile="chat", input_text="Долго"))
    await asyncio.wait_for(gateway.entered.wait(), 2)

    cancelled = await service.cancel(user_id=world.user, run_id=record.id)
    events = await collect(service, world.user, record.id)

    assert cancelled.status == "cancel_requested"
    assert types(events) == ["run.started", "run.cancelled"]
    assert payload_of(events, "run.cancelled") == {"reason": "user"}
    assert gateway.closed.is_set()
    final = await service.get(user_id=world.user, run_id=record.id)
    assert final.status == "cancelled" and final.response is None
    again = await service.cancel(user_id=world.user, run_id=record.id)
    assert again.status == "cancelled"


async def test_cancel_during_approval_wait(world: RunWorld) -> None:
    router, approvals = router_with(descriptor("test.send", risk="write_external"))
    gateway = scripted(tool_step("c1", "test.send", {"q": "x"}), text_step(answer("Ок.")))
    service = world.service(gateway, router=router, approvals=approvals)
    record, _ = await service.start(NewRun(user_id=world.user, profile="chat", input_text="Шли"))

    async for event in service.events(user_id=world.user, run_id=record.id):
        if event is not None and event.type == "approval.requested":
            assert (await service.get(user_id=world.user, run_id=record.id)).status == (
                "waiting_approval"
            )
            await service.cancel(user_id=world.user, run_id=record.id)
            break
    events = await collect(service, world.user, record.id)
    assert_well_formed(events)

    assert types(events)[-3:] == ["approval.resolved", "tool.call.completed", "run.cancelled"]
    assert payload_of(events, "approval.resolved")["status"] == "cancelled"
    assert payload_of(events, "tool.call.completed")["status"] == "cancelled"
    assert router.executed == []


async def test_cancel_requested_in_store_is_honoured_at_step_boundary(world: RunWorld) -> None:
    router, _ = router_with()
    store = world.store

    async def cancel_elsewhere(context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
        await store.request_cancel(uuid.UUID(context.run_id), user_id=world.user, reason="user")
        return ToolOutcome(call_id=request.call_id, status="succeeded", content="ok")

    router.register(descriptor("test.slow"), cancel_elsewhere)
    gateway = scripted(tool_step("c1", "test.slow", {}), text_step(answer("Не дойдёт.")))
    service = world.service(gateway, router=router)

    _, events = await run_to_end(service, world.user, profile="chat")

    assert types(events)[-2:] == ["tool.call.completed", "run.cancelled"]
    assert len(gateway.requests) == 1


async def test_time_budget_cancels_with_budget_exceeded(world: RunWorld) -> None:
    limits = RunLimits(
        voice=TEST_LIMITS.voice,
        chat=ProfileLimits(max_steps=3, max_tool_calls=1, timeout_s=0.2),
        job=TEST_LIMITS.job,
        app=TEST_LIMITS.app,
    )
    gateway = BlockingGateway(answer("Поздно."))
    service = world.service(gateway, limits=limits)

    _, events = await run_to_end(service, world.user, profile="chat")

    assert types(events) == ["run.started", "run.cancelled"]
    assert payload_of(events, "run.cancelled") == {"reason": "budget_exceeded"}


async def test_model_error_fails_run_with_public_code(world: RunWorld) -> None:
    gateway = scripted(ModelGatewayError("rate_limited", "provider said: secret body"))  # type: ignore[arg-type]
    service = world.service(gateway)

    run_id, events = await run_to_end(service, world.user, profile="chat")

    assert types(events) == ["run.started", "run.failed"]
    error = payload_of(events, "run.failed")["error"]
    assert error == {
        "code": "rate_limited",
        "message": "The model provider limit was reached.",
        "retryable": True,
    }
    assert (await service.get(user_id=world.user, run_id=run_id)).error == error


async def test_unconfigured_gateway_fails_after_run_started(world: RunWorld) -> None:
    service = world.service(UnconfiguredModelGateway())

    _, events = await run_to_end(service, world.user, profile="voice")

    assert payload_of(events, "run.started") == {"profile": "voice", "model": "mia:default"}
    assert payload_of(events, "run.failed")["error"]["code"] == "model_not_configured"


async def test_unexpected_error_is_internal_error(world: RunWorld) -> None:
    router, _ = router_with()

    class ExplodingRouter:
        async def list_tools(self, context: ToolContext) -> list[Any]:
            raise RuntimeError("database password=hunter2")

        async def call(self, context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
            raise AssertionError

    service = world.service(scripted(text_step("x")), router=ExplodingRouter())

    _, events = await run_to_end(service, world.user, profile="chat")

    assert payload_of(events, "run.failed")["error"]["code"] == "run_internal_error"
    assert "hunter2" not in json.dumps(events)


async def test_speech_ack_when_first_answer_is_slow(world: RunWorld) -> None:
    limits = RunLimits(
        voice=ProfileLimits(max_steps=3, max_tool_calls=2, timeout_s=5, ack_delay_ms=50),
        chat=TEST_LIMITS.chat,
        job=TEST_LIMITS.job,
        app=TEST_LIMITS.app,
    )
    slow = world.service(DelayedGateway(answer("Сегодня солнечно."), 0.3), limits=limits)
    _, events = await run_to_end(slow, world.user, profile="voice")
    assert types(events) == ["run.started", "speech.ack", "response.final", "run.completed"]
    assert payload_of(events, "speech.ack") == {"text": "Секунду.", "after_ms": 50}
    assert payload_of(events, "run.completed")["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": None,
    }

    fast = world.service(DelayedGateway(answer("Сразу."), 0), limits=limits, ack_text="Сек.")
    _, quick = await run_to_end(fast, world.user, profile="voice")
    await asyncio.sleep(0.1)
    assert "speech.ack" not in types(quick)


async def test_chat_plain_mode_streams_deltas_voice_does_not(world: RunWorld) -> None:
    chunks = ["Сегодня ", "три встречи. ", "Первая в 10:00."]
    chat = world.service(scripted(text_step(*chunks), info=PLAIN), delta_flush_chars=1)
    _, events = await run_to_end(chat, world.user, profile="chat")
    deltas = [e["payload"]["text"] for e in events if e["type"] == "message.delta"]
    assert "".join(deltas) == "".join(chunks)
    assert chat.engine.gateway.requests[0].structured_response is False  # type: ignore[attr-defined]

    voice = world.service(scripted(text_step(*chunks), info=PLAIN), delta_flush_chars=1)
    _, voice_events = await run_to_end(voice, world.user, profile="voice")
    assert "message.delta" not in types(voice_events)

    structured = world.service(scripted(text_step(answer("Ок."))), delta_flush_chars=1)
    _, structured_events = await run_to_end(structured, world.user, profile="chat")
    assert "message.delta" not in types(structured_events)


async def test_speech_display_in_both_model_modes(world: RunWorld) -> None:
    card = {"kind": "card", "title": "Встречи", "markdown": "- 10:00 Олег\n- 12:00 Яна"}
    structured = world.service(
        scripted(text_step(answer("Сегодня две встречи.", card, ["Перенести?"])))
    )
    _, events = await run_to_end(structured, world.user, profile="voice")
    assert payload_of(events, "response.final")["response"] == {
        "speech": "Сегодня две встречи.",
        "display": card,
        "followups": ["Перенести?"],
    }

    markdown = "Сегодня **две** встречи. Первая в 10:00 с Олегом.\n\n- [Олег](https://x.test/o)"
    plain = world.service(scripted(text_step(markdown), info=PLAIN))
    _, plain_events = await run_to_end(plain, world.user, profile="voice")
    assert payload_of(plain_events, "response.final")["response"] == {
        "speech": "Сегодня две встречи.",
        "display": {"kind": "text", "markdown": markdown},
        "followups": [],
    }

    broken = world.service(scripted(text_step("{not json. Просто текст.")))
    _, broken_events = await run_to_end(broken, world.user, profile="chat")
    assert payload_of(broken_events, "response.final")["response"]["speech"] == "{not json."


async def test_memory_facts_reach_the_system_prompt(world: RunWorld) -> None:
    memory = FakeMemoryProvider()
    fact = FactRecord(
        id=str(uuid.uuid4()),
        text="Любит кофе без сахара",
        category="profile",
        status="active",
        sensitivity="normal",
        source=MemorySource(type="user_said"),
    )
    memory.add_fact(str(world.user), fact)
    gateway = scripted(text_step(answer("Ок.")), text_step(answer("Ок.")))
    service = world.service(gateway, memory=memory)

    await run_to_end(service, world.user, text="кофе", profile="chat")
    await run_to_end(service, world.user, text="кофе", profile="chat", incognito=True)

    assert "Любит кофе без сахара" in gateway.requests[0].messages[0].content
    assert "Любит кофе без сахара" not in gateway.requests[1].messages[0].content
    assert memory.used == [fact.id]


async def test_publish_external_events_into_live_run(world: RunWorld) -> None:
    gateway = BlockingGateway(answer("Ок."))
    service = world.service(gateway)
    record, _ = await service.start(NewRun(user_id=world.user, profile="chat", input_text="Ок"))
    await asyncio.wait_for(gateway.entered.wait(), 2)

    await service.publish(
        run_id=record.id,
        event_type="job.created",
        payload={"job_id": str(uuid.uuid4()), "goal": "Собрать карту"},
    )
    with pytest.raises(ValueError):
        await service.publish(run_id=record.id, event_type="run.completed", payload={})
    gateway.release.set()
    events = await collect(service, world.user, record.id)
    assert_well_formed(events)
    assert types(events) == ["run.started", "job.created", "response.final", "run.completed"]
    with pytest.raises(RunFinishedError):
        await service.publish(
            run_id=record.id,
            event_type="memory.suggested",
            payload={"fact_id": str(uuid.uuid4()), "text": "x", "category": "other"},
        )


async def test_orphaned_run_is_failed_or_cancelled_on_access(world: RunWorld) -> None:
    gateway = scripted(text_step(answer("Ок.")))
    service = world.service(gateway)
    reaper = RunService(service.engine, orphan_after_s=0, keepalive_s=0.05)

    lost, _ = await world.store.create(NewRun(user_id=world.user, profile="chat", input_text="x"))
    events = await collect(reaper, world.user, lost.id)
    assert types(events) == ["run.started", "run.failed"]
    assert payload_of(events, "run.failed")["error"]["code"] == "run_interrupted"

    other, _ = await world.store.create(NewRun(user_id=world.user, profile="chat", input_text="y"))
    await reaper.cancel(user_id=world.user, run_id=other.id)
    assert types(await collect(reaper, world.user, other.id)) == ["run.started", "run.cancelled"]


async def test_foreign_user_cannot_see_or_cancel(world: RunWorld) -> None:
    from app.modules.runs import RunNotFoundError

    service = world.service(scripted(text_step(answer("Ок."))))
    run_id, _ = await run_to_end(service, world.user, profile="chat")
    stranger = world.users[1]

    with pytest.raises(RunNotFoundError):
        await service.get(user_id=stranger, run_id=run_id)
    with pytest.raises(RunNotFoundError):
        await service.cancel(user_id=stranger, run_id=run_id)
    with pytest.raises(RunNotFoundError):
        await collect(service, stranger, run_id)


async def test_idempotent_start_replays_without_second_execution(world: RunWorld) -> None:
    from app.modules.conversations.repositories import IdempotencyConflictError

    gateway = scripted(text_step(answer("Ок.")))
    service = world.service(gateway)
    new = NewRun(user_id=world.user, profile="chat", input_text="Раз")
    first, replayed = await service.start(new, idempotency_key="k" * 20, request_hash="h1")
    await collect(service, world.user, first.id)
    again, replayed_again = await service.start(
        NewRun(user_id=world.user, profile="chat", input_text="Раз"),
        idempotency_key="k" * 20,
        request_hash="h1",
    )

    assert (replayed, replayed_again) == (False, True)
    assert again.id == first.id and len(gateway.requests) == 1
    with pytest.raises(IdempotencyConflictError):
        await service.start(new, idempotency_key="k" * 20, request_hash="h2")
