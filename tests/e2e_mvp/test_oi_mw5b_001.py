"""OI-MW5b-001 (M60): what the load runs found.

Under CPU load ``tests/runs/test_engine.py::test_no_speech_ack_while_waiting_for_approval``
failed: the «Секунду.» filler landed after ``approval.requested``. The ack timer checked
``approval_requested`` under ``final_lock`` and then wrote the event, while ``approve()`` set
the flag without that lock — so a slow ``speech.ack`` write could finish after the approval
prompt and the phone would say «Секунду.» over the confirmation (OI-M22-002). This test makes
the ack write slow deterministically instead of relying on load.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any

from app.core.ports.model_gateway import ModelEvent, ModelInfo, ModelRequest
from app.core.ports.types import DEFAULT_MODEL
from app.modules.runs import (
    EngineConfig,
    InMemoryRunStore,
    NewRun,
    RunEngine,
    RunNotifier,
    RunService,
)
from app.modules.runs.domain import StoredEvent
from app.modules.runs.profiles import ProfileLimits, RunLimits

from tests.fakes import text_step, tool_step
from tests.runs.conftest import TEST_LIMITS
from tests.runs.gateways import STRUCTURED, answer
from tests.runs.tools import descriptor, router_with


class SlowAckStore(InMemoryRunStore):
    """``speech.ack`` is written only after ``gate`` opens (a slow database under load)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.ack_writing = asyncio.Event()

    async def append(
        self, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any], *, at: datetime
    ) -> StoredEvent:
        if event_type == "speech.ack":
            self.ack_writing.set()
            await self.gate.wait()
        return await super().append(run_id, event_type, payload, at=at)


class SlowFirstStep:
    """The first model step (a ``write_external`` call) arrives after the ack timer fired."""

    def __init__(self) -> None:
        self.info: ModelInfo = STRUCTURED
        self.steps = [
            tool_step("c1", "test.send", {"q": "привет"}),
            text_step(answer("Отправила.")),
        ]

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        return self.info

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        step = self.steps.pop(0)
        if len(self.steps) == 1:
            await asyncio.sleep(0.15)
        for event in step:
            yield event


async def test_no_filler_after_the_approval_prompt_when_the_ack_write_is_slow() -> None:
    limits = RunLimits(
        voice=ProfileLimits(max_steps=3, max_tool_calls=2, timeout_s=5, ack_delay_ms=20),
        chat=TEST_LIMITS.chat,
        job=TEST_LIMITS.job,
        app=TEST_LIMITS.app,
    )
    store = SlowAckStore()
    router, approvals = router_with(descriptor("test.send", risk="write_external"))
    engine = RunEngine(
        store=store,
        notifier=RunNotifier(),
        gateway=SlowFirstStep(),
        config=EngineConfig(limits=limits),
        router=router,
        approvals=approvals,
    )
    service = RunService(engine, keepalive_s=0.2)
    user = uuid.uuid4()
    record, _ = await service.start(NewRun(user_id=user, profile="voice", input_text="Отправь"))

    await asyncio.wait_for(store.ack_writing.wait(), 2)
    await asyncio.sleep(0.4)  # the model's tool call is back; approve() runs meanwhile
    store.gate.set()

    async def decide() -> None:
        async for event in service.events(user_id=user, run_id=record.id):
            if event is not None and event.type == "approval.requested":
                approval = event.payload["approval"]
                await approvals.decide(
                    user_id=str(user),
                    approval_id=approval["id"],
                    decision="approve",
                    args_hash=approval["args_hash"],
                    channel="android",
                )
                return

    await asyncio.wait_for(decide(), 5)
    await service.wait_finished(record.id)
    kinds = [event.type async for event in service.events(user_id=user, run_id=record.id) if event]
    await service.aclose()

    assert "approval.requested" in kinds and kinds[-1] == "run.completed"
    if "speech.ack" in kinds:
        assert kinds.index("speech.ack") < kinds.index("approval.requested"), kinds
