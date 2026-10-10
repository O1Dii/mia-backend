"""Bounded run loop: context → ModelGateway → ToolRouter → final answer (ADR-0011).

One ``RunExecution`` drives one run and is the only writer of its lifecycle
events. Guarantees:

- events are appended through ``RunStore.append`` (gap-free ``sequence``) and
  exactly one terminal event ends the run;
- the profile's step budget is enforced by offering no tools on the last allowed
  step, and the tool-call budget by skipping calls over it;
- cancellation aborts model streaming and approval waits at once; a tool call in
  flight is allowed to finish (an external write must not be torn);
- time spent waiting for a human approval does not count against the budget;
- a tool's screen-only action (M41-R1) goes to ``tool.call.completed`` and to the
  feed entry of the answer, never into a message the model sees;
- ``tool.call.started`` carries the redacted arguments and ``tool.call.completed`` the
  reason of a failure (``error_detail``), so the tool-call journal is readable (M73).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol, TypeVar

from app.core.ports.approval_service import Approval, ApprovalService
from app.core.ports.memory_provider import FactRecord, MemoryProvider
from app.core.ports.model_gateway import (
    ModelDone,
    ModelGateway,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ModelUsage,
    TextDelta,
    ToolCallRequested,
)
from app.core.ports.screen_action import parse_action
from app.core.ports.tool_router import (
    ToolCallRequest,
    ToolCallStatus,
    ToolContext,
    ToolDescriptor,
    ToolOutcome,
    ToolRouter,
)
from app.core.ports.types import DEFAULT_MODEL
from app.modules.conversations.feed import FeedContext, FeedSummarizer, FeedTurn
from app.modules.conversations.models import Modality, TurnSource, TurnStatus
from app.modules.runs.domain import (
    CancelReason,
    RunFinishedError,
    RunRecord,
    StoredEvent,
    isoformat,
    utc_now,
)
from app.modules.runs.errors import public_error, tool_error
from app.modules.runs.notifier import RunNotifier
from app.modules.runs.profiles import ProfileLimits, RunLimits
from app.modules.runs.prompts import system_instruction
from app.modules.runs.redaction import failure_reason, redact_arguments
from app.modules.runs.response import ParsedResponse, clip, parse_response
from app.modules.runs.store import RunStore

logger = logging.getLogger(__name__)

T = TypeVar("T")
DELTA_MAX_CHARS = 8000
_UI_URI_PREFIX = "ui://"
MAX_SCREEN_ACTIONS = 4
_TOOL_STATUSES = frozenset({"succeeded", "failed", "denied", "expired", "uncertain", "cancelled"})


class RunFeed(Protocol):
    """What the engine needs from ``conversations.FeedService``."""

    async def owns_conversation(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> bool: ...

    async def context(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID | None = None
    ) -> FeedContext: ...

    async def open_turn(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        source: TurnSource,
        input_text: str,
        modality: Modality = Modality.TEXT,
    ) -> FeedTurn: ...

    async def record_answer(
        self,
        *,
        user_id: uuid.UUID,
        turn: FeedTurn,
        text_value: str,
        response: dict[str, Any],
        modality: Modality = Modality.TEXT,
        actions: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """``actions``: screen-only actions of the run's tool calls (M41-R1), kept with
        the answer for the UI and never part of the model's history."""
        ...

    async def close_turn(self, *, turn: FeedTurn, status: TurnStatus) -> None: ...

    async def summarize_if_needed(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID, summarizer: FeedSummarizer
    ) -> bool: ...


class CancelSignal:
    """In-process cancellation request for one live run."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: CancelReason = "user"

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self, reason: CancelReason = "user") -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class _RunCancelled(Exception):
    def __init__(self, reason: CancelReason) -> None:
        super().__init__(reason)
        self.reason = reason


class _BudgetExceeded(Exception):
    pass


@dataclass(slots=True)
class _Step:
    text: str = ""
    calls: list[ModelToolCall] = field(default_factory=list)


@dataclass(slots=True)
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    seen: bool = False

    def add(self, usage: ModelUsage) -> None:
        self.seen = True
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if usage.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + usage.cost_usd

    def payload(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
        }


Background = Callable[[Coroutine[Any, Any, Any]], None]
SummarizerFactory = Callable[[uuid.UUID], FeedSummarizer]


@dataclass(frozen=True, slots=True)
class EngineConfig:
    limits: RunLimits
    ack_text: str = "Секунду."
    structured_output: bool = True
    """Use structured output when the model supports it (else heuristic split)."""
    heartbeat_s: float = 10.0
    delta_flush_chars: int = 160
    delta_flush_s: float = 0.15


class RunEngine:
    def __init__(
        self,
        *,
        store: RunStore,
        notifier: RunNotifier,
        gateway: ModelGateway,
        config: EngineConfig,
        router: ToolRouter | None = None,
        approvals: ApprovalService | None = None,
        memory: MemoryProvider | None = None,
        feed: RunFeed | None = None,
        summarizer_factory: SummarizerFactory | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.gateway = gateway
        self.config = config
        self.router = router
        self.approvals = approvals
        self.memory = memory
        self.feed = feed
        self.summarizer_factory = summarizer_factory
        self.clock = clock

    async def run(
        self,
        record: RunRecord,
        input_text: str,
        signal: CancelSignal,
        *,
        background: Background | None = None,
    ) -> None:
        await RunExecution(self, record, input_text, signal, background).run()


class RunExecution:
    def __init__(
        self,
        engine: RunEngine,
        record: RunRecord,
        input_text: str,
        signal: CancelSignal,
        background: Background | None,
    ) -> None:
        self.engine = engine
        self.record = record
        self.input_text = input_text
        self.signal = signal
        self.background = background
        self.limits: ProfileLimits = engine.config.limits.for_profile(record.profile)
        self.started_at = time.monotonic()
        self.deadline = self.started_at + self.limits.timeout_s
        self.model_ref = DEFAULT_MODEL
        self.structured = False
        self.started = False
        self.final_emitted = False
        self.approval_requested = False
        """No ``speech.ack`` after an approval prompt: the client speaks the prompt (OI-M22-002)."""
        self.final_lock = asyncio.Lock()
        self.steps = 0
        self.tool_calls = 0
        self.usage = _Usage()
        self.descriptors: dict[str, ToolDescriptor] = {}
        self.seen_calls: dict[str, tuple[str, str]] = {}
        self.actions: list[dict[str, Any]] = []
        """Screen-only actions of succeeded calls, stored with the answer (M41-R1)."""
        self.conversation_id: uuid.UUID | None = None
        self.turn: FeedTurn | None = None
        self.locale = "en"
        self.timezone = "UTC"
        self.summary: str | None = None
        self.history: list[ModelMessage] = []
        self.facts: Sequence[FactRecord] = ()

    # -- events -----------------------------------------------------------------

    async def emit(self, event_type: str, payload: Mapping[str, Any]) -> StoredEvent:
        event = await self.engine.store.append(
            self.record.id, event_type, payload, at=self.engine.clock()
        )
        self.engine.notifier.notify(self.record.id)
        return event

    async def ensure_started(self) -> None:
        if self.started:
            return
        payload: dict[str, Any] = {"profile": self.record.profile, "model": self.model_ref}
        if self.conversation_id is not None and not self.record.incognito:
            payload["conversation_id"] = str(self.conversation_id)
        await self.emit("run.started", payload)
        self.started = True

    # -- lifecycle --------------------------------------------------------------

    async def run(self) -> None:
        helpers: list[asyncio.Task[None]] = []
        outcome: TurnStatus = TurnStatus.FAILED
        try:
            try:
                await self.prepare()
                await self.ensure_started()
                helpers.append(asyncio.create_task(self.heartbeat()))
                if self.limits.ack_delay_ms is not None:
                    helpers.append(asyncio.create_task(self.ack_after(self.limits.ack_delay_ms)))
                text = await self.loop()
                parsed = parse_response(
                    text, structured=self.structured, speech_max_chars=self.limits.speech_max_chars
                )
                await self.check_cancel()
                async with self.final_lock:
                    await self.emit("response.final", {"response": parsed.response})
                    self.final_emitted = True
                await self.record_answer(parsed)
                outcome = TurnStatus.COMPLETED
                completed: dict[str, Any] = {"steps": self.steps, "tool_calls": self.tool_calls}
                if self.usage.seen:
                    completed["usage"] = self.usage.payload()
                await self.emit("run.completed", completed)
                self.schedule_summary()
            except _RunCancelled as cancelled:
                outcome = TurnStatus.INTERRUPTED
                await self.terminate("run.cancelled", {"reason": cancelled.reason})
            except _BudgetExceeded:
                outcome = TurnStatus.INTERRUPTED
                await self.terminate("run.cancelled", {"reason": "budget_exceeded"})
            except ModelGatewayError as error:
                await self.terminate("run.failed", {"error": public_error(error.code)})
            except RunFinishedError:
                logger.info("run_finished_elsewhere", extra={"run_id": str(self.record.id)})
            except asyncio.CancelledError:
                await asyncio.shield(
                    self.terminate("run.failed", {"error": public_error("run_interrupted")})
                )
                raise
            except Exception:
                logger.exception("run_failed", extra={"run_id": str(self.record.id)})
                await self.terminate("run.failed", {"error": public_error("run_internal_error")})
        finally:
            for helper in helpers:
                helper.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
            if self.turn is not None and outcome != TurnStatus.COMPLETED:
                await self.close_turn(outcome)

    async def terminate(self, event_type: str, payload: Mapping[str, Any]) -> None:
        try:
            await self.ensure_started()
            await self.emit(event_type, payload)
        except RunFinishedError:
            logger.info("run_finished_elsewhere", extra={"run_id": str(self.record.id)})

    async def prepare(self) -> None:
        engine = self.engine
        user = self.record.user_id
        info = await self.guard(engine.gateway.describe(user_id=str(user)))
        self.model_ref = info.ref
        self.structured = engine.config.structured_output and info.supports_structured_output
        if engine.feed is not None:
            context = await engine.feed.context(
                user_id=user, conversation_id=self.record.conversation_id
            )
            self.locale, self.timezone, self.summary = (
                context.locale,
                context.timezone,
                context.summary,
            )
            self.history = [ModelMessage(role=m.role, content=m.text) for m in context.messages]
            if not self.record.incognito:
                self.conversation_id = context.conversation_id
                voice = self.record.profile == "voice"
                self.turn = await engine.feed.open_turn(
                    user_id=user,
                    conversation_id=context.conversation_id,
                    run_id=self.record.id,
                    source=TurnSource(f"run_{self.record.profile}"),
                    input_text=self.input_text,
                    modality=Modality.AUDIO if voice else Modality.TEXT,
                )
                await engine.store.link_turn(
                    self.record.id,
                    conversation_id=self.turn.conversation_id,
                    turn_id=self.turn.turn_id,
                )
        if engine.memory is not None and not self.record.incognito:
            self.facts = await engine.memory.context(user_id=str(user), query=self.input_text)
            if self.facts:
                await engine.memory.mark_used(
                    user_id=str(user), record_ids=[fact.id for fact in self.facts]
                )
        if engine.router is not None:
            listed = await engine.router.list_tools(self.tool_context())
            self.descriptors = {
                descriptor.spec.name: descriptor
                for descriptor in listed
                if self.record.profile != "voice" or descriptor.voice
            }

    # -- loop -------------------------------------------------------------------

    async def loop(self) -> str:
        instruction = system_instruction(
            profile=self.record.profile,
            structured=self.structured,
            speech_limit=self.limits.speech_max_chars,
            now=self.engine.clock(),
            locale=self.locale,
            timezone=self.timezone,
            summary=self.summary,
            facts=self.facts,
        )
        messages: list[ModelMessage] = [
            ModelMessage(role="system", content=instruction),
            *self.history,
            ModelMessage(role="user", content=self.input_text),
        ]
        specs = [descriptor.spec for descriptor in self.descriptors.values()]
        while True:
            await self.check_cancel()
            offer_tools = (
                bool(specs)
                and self.steps + 1 < self.limits.max_steps
                and self.tool_calls < self.limits.max_tool_calls
            )
            request = ModelRequest(
                user_id=str(self.record.user_id),
                profile=self.record.profile,
                messages=tuple(messages),
                tools=tuple(specs) if offer_tools else (),
                structured_response=self.structured,
            )
            step = await self.guard(self.model_step(request))
            self.steps += 1
            if not step.calls or not offer_tools:
                return step.text
            messages.append(
                ModelMessage(role="assistant", content=step.text, tool_calls=tuple(step.calls))
            )
            for call in step.calls:
                content = await self.call_tool(call)
                messages.append(
                    ModelMessage(role="tool", content=content, tool_call_id=call.call_id)
                )

    async def model_step(self, request: ModelRequest) -> _Step:
        step = _Step()
        parts: list[str] = []
        stream_deltas = self.limits.stream_deltas and not self.structured
        buffer = ""
        last_flush = time.monotonic()
        stream = self.engine.gateway.stream(request)
        try:
            async for event in stream:
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                    if stream_deltas:
                        buffer += event.text
                        due = time.monotonic() - last_flush >= self.engine.config.delta_flush_s
                        if len(buffer) >= self.engine.config.delta_flush_chars or due:
                            await self.flush_delta(buffer)
                            buffer, last_flush = "", time.monotonic()
                elif isinstance(event, ToolCallRequested):
                    step.calls.append(event.call)
                elif isinstance(event, ModelUsage):
                    self.usage.add(event)
                elif isinstance(event, ModelDone):
                    break
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        if buffer:
            await self.flush_delta(buffer)
        step.text = "".join(parts)
        return step

    async def flush_delta(self, text: str) -> None:
        for start in range(0, len(text), DELTA_MAX_CHARS):
            chunk = text[start : start + DELTA_MAX_CHARS]
            if chunk:
                await self.emit("message.delta", {"text": chunk})

    # -- tools ------------------------------------------------------------------

    def tool_context(self) -> ToolContext:
        record = self.record
        return ToolContext(
            user_id=str(record.user_id),
            run_id=str(record.id),
            profile=record.profile,
            workspace_id=record.workspace_id,
            device_id=str(record.device_id) if record.device_id else None,
            app_client_id=record.app_client_id,
            scopes=record.scopes,
        )

    async def call_tool(self, call: ModelToolCall) -> str:
        signature = json.dumps([call.name, call.arguments], sort_keys=True, ensure_ascii=False)
        previous = self.seen_calls.get(call.call_id)
        if previous is not None:
            if previous[0] == signature:
                return previous[1]
            return _model_result("failed", error="duplicate_call_id")
        descriptor = self.descriptors.get(call.name)
        if descriptor is None:
            logger.warning(
                "tool_call_unknown",
                extra={"run_id": str(self.record.id), "tool_name": call.name[:128]},
            )
            return _model_result("failed", error="tool_not_found")
        if self.tool_calls >= self.limits.max_tool_calls:
            return _model_result("skipped", error="tool_budget_exceeded")
        self.tool_calls += 1
        started: dict[str, Any] = {
            "tool_call_id": call.call_id,
            "tool": call.name,
            "risk": descriptor.risk,
            "executor": descriptor.executor,
            "arguments": redact_arguments(call.arguments, device=descriptor.executor == "device"),
        }
        if descriptor.plugin_id is not None:
            started["plugin_id"] = descriptor.plugin_id
        await self.emit("tool.call.started", started)
        context = self.tool_context()
        request = ToolCallRequest(
            call_id=call.call_id, tool=call.name, arguments=dict(call.arguments)
        )
        assert self.engine.router is not None
        outcome = await self.engine.router.call(context, request)
        if outcome.status == "approval_required":
            try:
                outcome = await self.approve(context, request, outcome)
            except _RunCancelled:
                await self.emit(
                    "tool.call.completed",
                    {"tool_call_id": call.call_id, "status": "cancelled"},
                )
                raise
        payload = _completed_payload(call.call_id, outcome)
        if "action" in payload and len(self.actions) < MAX_SCREEN_ACTIONS:
            self.actions.append(payload["action"])
        await self.emit("tool.call.completed", payload)
        content = _model_result(
            outcome.status,
            content=outcome.content,
            error=None if outcome.status == "succeeded" else (outcome.error_code or None),
        )
        self.seen_calls[call.call_id] = (signature, content)
        return content

    async def approve(
        self, context: ToolContext, request: ToolCallRequest, outcome: ToolOutcome
    ) -> ToolOutcome:
        approvals = self.engine.approvals
        approval = outcome.approval
        if approvals is None or approval is None:
            return ToolOutcome(
                call_id=request.call_id, status="denied", error_code="approval_unavailable"
            )
        async with self.final_lock:
            # ``ack_after`` checks the flag and writes under this lock: a slow «Секунду.» write
            # finishes before the prompt or not at all (OI-M22-002, found under load in M60).
            self.approval_requested = True
        await self.emit("approval.requested", {"approval": approval_payload(approval)})
        remaining = max(0.0, (approval.expires_at - self.engine.clock()).total_seconds())
        waited_from = time.monotonic()
        try:
            current = await self.guard(
                approvals.wait(
                    user_id=context.user_id, approval_id=approval.id, timeout_s=remaining
                ),
                deadline=False,
            )
        except _RunCancelled:
            await self.emit(
                "approval.resolved", {"approval_id": approval.id, "status": "cancelled"}
            )
            raise
        finally:
            self.deadline += time.monotonic() - waited_from
        status = current.status if current.status != "pending" else "expired"
        await self.emit("approval.resolved", {"approval_id": approval.id, "status": status})
        if status != "approved":
            mapped: ToolCallStatus = (
                "denied"
                if status == "denied"
                else "expired"
                if status == "expired"
                else "cancelled"
            )
            return ToolOutcome(
                call_id=request.call_id, status=mapped, error_code=f"approval_{status}"
            )
        router = self.engine.router
        assert router is not None
        second = await router.call(context, replace(request, approval_id=approval.id))
        if second.status == "approval_required":
            return ToolOutcome(
                call_id=request.call_id, status="denied", error_code="approval_denied"
            )
        return second

    # -- guards -----------------------------------------------------------------

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    async def guard(self, awaitable: Awaitable[T], *, deadline: bool = True) -> T:
        """Await ``awaitable`` unless cancellation or the time budget comes first."""
        if self.signal.requested:
            raise _RunCancelled(self.signal.reason)
        task: asyncio.Future[T] = asyncio.ensure_future(awaitable)
        cancel = asyncio.ensure_future(self.signal.wait())
        waiters: set[asyncio.Future[Any]] = {task, cancel}
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=self.remaining() if deadline else None,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            task.cancel()
            cancel.cancel()
            raise
        cancel.cancel()
        if task in done:
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self.signal.requested:
            raise _RunCancelled(self.signal.reason)
        raise _BudgetExceeded()

    async def check_cancel(self) -> None:
        if not self.signal.requested:
            current = await self.engine.store.get(self.record.id)
            if current is not None and current.status == "cancel_requested":
                self.signal.request("user")
        if self.signal.requested:
            raise _RunCancelled(self.signal.reason)
        if self.remaining() <= 0:
            raise _BudgetExceeded()

    # -- helpers ----------------------------------------------------------------

    async def ack_after(self, delay_ms: int) -> None:
        await asyncio.sleep(delay_ms / 1000)
        async with self.final_lock:
            if self.final_emitted or self.approval_requested:
                return
            try:
                await self.emit(
                    "speech.ack",
                    {"text": clip(self.engine.config.ack_text, 120), "after_ms": delay_ms},
                )
            except RunFinishedError:
                return

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.engine.config.heartbeat_s)
            try:
                await self.engine.store.heartbeat(self.record.id, at=self.engine.clock())
            except Exception:
                logger.warning("run_heartbeat_failed", extra={"run_id": str(self.record.id)})

    async def record_answer(self, parsed: ParsedResponse) -> None:
        feed, turn = self.engine.feed, self.turn
        if feed is None or turn is None:
            return
        try:
            await feed.record_answer(
                user_id=self.record.user_id,
                turn=turn,
                text_value=parsed.feed_text,
                response=parsed.response,
                modality=Modality.AUDIO if self.record.profile == "voice" else Modality.TEXT,
                actions=tuple(self.actions),
            )
        except Exception:
            logger.warning("run_feed_write_failed", extra={"run_id": str(self.record.id)})

    async def close_turn(self, status: TurnStatus) -> None:
        if self.engine.feed is None or self.turn is None:
            return
        try:
            await asyncio.shield(self.engine.feed.close_turn(turn=self.turn, status=status))
        except Exception:
            logger.warning("run_feed_close_failed", extra={"run_id": str(self.record.id)})

    def schedule_summary(self) -> None:
        engine = self.engine
        if (
            self.background is None
            or engine.feed is None
            or engine.summarizer_factory is None
            or self.turn is None
        ):
            return
        self.background(
            engine.feed.summarize_if_needed(
                user_id=self.record.user_id,
                conversation_id=self.turn.conversation_id,
                summarizer=engine.summarizer_factory(self.record.user_id),
            )
        )


def _model_result(status: str, *, content: str = "", error: str | None = None) -> str:
    """Tool result as the model sees it; ``structured_content`` never goes here."""
    body: dict[str, Any] = {"status": status}
    if content:
        body["content"] = content
    if error:
        body["error"] = error
    return json.dumps(body, ensure_ascii=False)


def _completed_payload(call_id: str, outcome: ToolOutcome) -> dict[str, Any]:
    status = outcome.status if outcome.status in _TOOL_STATUSES else "failed"
    payload: dict[str, Any] = {"tool_call_id": call_id, "status": status}
    if outcome.structured_content is not None:
        payload["structured_content"] = dict(outcome.structured_content)
    uri = outcome.ui_resource_uri
    if uri and uri.startswith(_UI_URI_PREFIX) and len(uri) <= 512:
        payload["ui_resource_uri"] = uri
    action = parse_action(outcome.action) if status == "succeeded" else None
    if action is not None:
        payload["action"] = action
    if status != "succeeded":
        payload["error"] = tool_error(status, outcome.error_code)
        reason = failure_reason(outcome.content)
        if reason is not None:
            payload["error_detail"] = reason
    return payload


def approval_payload(approval: Approval) -> dict[str, Any]:
    """``approval.schema.json#/$defs/Approval``."""
    body: dict[str, Any] = {
        "id": approval.id,
        "run_id": approval.run_id,
        "tool_call_id": approval.tool_call_id,
        "tool": approval.tool,
        "risk": approval.risk,
        "summary": clip(approval.summary, 500),
        "arguments": json.loads(json.dumps(dict(approval.arguments), ensure_ascii=False)),
        "args_hash": approval.args_hash,
        "status": approval.status,
        "created_at": isoformat(approval.created_at),
        "expires_at": isoformat(approval.expires_at),
        "decision": None,
    }
    if approval.plugin_id is not None:
        body["plugin_id"] = approval.plugin_id
    if approval.decision is not None and approval.status in ("approved", "denied"):
        decision: dict[str, Any] = {
            "decision": approval.decision.decision,
            "decided_at": isoformat(approval.decision.decided_at),
            "channel": approval.decision.channel,
        }
        if approval.decision.device_id is not None:
            decision["device_id"] = approval.decision.device_id
        body["decision"] = decision
    if approval.device_consent is not None:
        body["device_consent"] = json.loads(json.dumps(dict(approval.device_consent)))
    return body
