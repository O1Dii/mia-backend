"""Bounded realtime WebSocket state machine."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from typing import cast
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from app.core.metrics import METRICS
from app.modules.realtime.contracts import (
    INPUT_AUDIO_FORMAT,
    OUTPUT_AUDIO_FORMAT,
    InputAudioCommit,
    InputAudioStart,
    Ping,
    ResponseCancel,
    SessionClose,
    SessionConfigure,
    parse_client_event,
)
from app.modules.realtime.provider import (
    ProviderAudio,
    ProviderControl,
    ProviderOutput,
    RealtimeProviderFactory,
    RealtimeProviderSession,
)
from app.modules.realtime.sessions import (
    InMemoryRealtimeSessionStore,
    RealtimeAuthenticator,
    RealtimeSession,
    RealtimeSessionNotFoundError,
)
from app.modules.tools import ToolExecutor, ToolPrincipal

logger = logging.getLogger(__name__)
_EXPIRY_GRACE_SECONDS = 5.0
# A healthy client streams microphone frames continuously while it is listening, so
# silence this long means the socket is dead rather than quiet. Without it the server
# keeps a vanished client's session, and with one active session per user that blocks
# the next voice attempt until the provider's own idle timeout fires.
_CLIENT_INACTIVITY_SECONDS = 30.0


class ConnectionPhase(StrEnum):
    AWAITING_CONFIGURATION = "awaiting_configuration"
    READY = "ready"
    INPUT_OPEN = "input_open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class _AudioCommand:
    data: bytes


@dataclass(frozen=True, slots=True)
class _CommitCommand:
    stream_id: UUID
    turn_id: UUID


@dataclass(frozen=True, slots=True)
class _CancelCommand:
    response_id: UUID
    reason: str


InputCommand = _AudioCommand | _CommitCommand | _CancelCommand


class _RealtimeConnection:
    def __init__(
        self,
        *,
        websocket: WebSocket,
        session: RealtimeSession,
        provider: RealtimeProviderSession,
        store: InMemoryRealtimeSessionStore,
        input_queue_size: int,
        output_queue_size: int,
        max_binary_frame_bytes: int,
        heartbeat_interval_ms: int,
        backpressure_timeout_seconds: float,
        tool_executor: ToolExecutor | None = None,
        request_id: str | None = None,
    ) -> None:
        self._websocket = websocket
        self._session = session
        self._provider = provider
        self._store = store
        self._tool_executor = tool_executor
        self._tool_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._tool_responses: dict[UUID, UUID] = {}
        self._phase = ConnectionPhase.AWAITING_CONFIGURATION
        self._expected_client_seq = 1
        self._server_seq = 0
        self._input_queue: asyncio.Queue[InputCommand] = asyncio.Queue(maxsize=input_queue_size)
        self._output_queue: asyncio.Queue[ProviderOutput] = asyncio.Queue(maxsize=output_queue_size)
        self._max_binary_frame_bytes = max_binary_frame_bytes
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._backpressure_timeout = backpressure_timeout_seconds
        self._send_lock = asyncio.Lock()
        self._configured_capabilities: set[str] = set()
        self._open_stream_id: UUID | None = None
        self._open_stream_turn_id: UUID | None = None
        self._received_audio_bytes = 0
        self._cancelled_responses: set[UUID] = set()
        self._response_turns: dict[UUID, UUID] = {}
        self._failed = False
        self._normal_close = False
        self._client_inactive = False
        self._last_server_send = time.monotonic()
        self._request_id = request_id or f"ws_{uuid4().hex}"

    def _event(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        turn_id: UUID | None = None,
        response_id: UUID | None = None,
    ) -> dict[str, object]:
        self._server_seq += 1
        event: dict[str, object] = {
            "v": 1,
            "type": event_type,
            "session_id": str(self._session.id),
            "seq": self._server_seq,
            "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "payload": payload,
        }
        if turn_id is not None:
            event["turn_id"] = str(turn_id)
        if response_id is not None:
            event["response_id"] = str(response_id)
        return event

    async def _send_control(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        turn_id: UUID | None = None,
        response_id: UUID | None = None,
    ) -> None:
        async with self._send_lock:
            self._last_server_send = time.monotonic()
            await self._websocket.send_json(
                self._event(
                    event_type,
                    payload,
                    turn_id=turn_id,
                    response_id=response_id,
                )
            )

    async def _fail(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        close_code: int = 1002,
    ) -> None:
        if self._failed:
            return
        self._failed = True
        self._phase = ConnectionPhase.CLOSING
        try:
            await self._send_control(
                "error",
                {
                    "code": code,
                    "message": message,
                    "request_id": self._request_id,
                    "retryable": retryable,
                },
            )
        finally:
            if self._websocket.application_state is WebSocketState.CONNECTED:
                await self._websocket.close(code=close_code)

    def _accept_client_sequence(self, session_id: UUID, seq: int) -> bool:
        if session_id != self._session.id or seq != self._expected_client_seq:
            return False
        self._expected_client_seq += 1
        return True

    async def _configure(self, event: SessionConfigure) -> bool:
        if self._phase is not ConnectionPhase.AWAITING_CONFIGURATION:
            await self._fail("realtime_invalid_event", "Session is already configured.")
            return False
        payload = event.payload
        if (
            payload.conversation_id != self._session.conversation_id
            or INPUT_AUDIO_FORMAT not in payload.input_audio
            or OUTPUT_AUDIO_FORMAT not in payload.output_audio
        ):
            await self._fail(
                "realtime_protocol_unsupported",
                "Required PCM audio formats were not offered.",
            )
            return False
        try:
            await self._provider.configure(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail(
                "realtime_provider_unavailable",
                "Realtime provider is unavailable.",
                retryable=True,
                close_code=1011,
            )
            return False
        self._configured_capabilities = set(payload.capabilities)
        self._phase = ConnectionPhase.READY
        await self._send_control(
            "session.ready",
            {
                "protocol_version": 1,
                "input_audio": INPUT_AUDIO_FORMAT.model_dump(mode="json"),
                "output_audio": OUTPUT_AUDIO_FORMAT.model_dump(mode="json"),
                "max_binary_frame_bytes": self._max_binary_frame_bytes,
                "heartbeat_interval_ms": self._heartbeat_interval_ms,
            },
        )
        return True

    async def _queue_input(self, command: InputCommand) -> bool:
        try:
            self._input_queue.put_nowait(command)
            return True
        except asyncio.QueueFull:
            await self._fail(
                "realtime_backpressure",
                "Realtime input queue is full.",
                retryable=True,
                close_code=1013,
            )
            return False

    async def _handle_control(self, raw: str) -> bool:
        try:
            event = parse_client_event(raw)
        except (ValidationError, ValueError):
            await self._fail("realtime_invalid_event", "Invalid realtime control event.")
            return False
        if not self._accept_client_sequence(event.session_id, event.seq):
            await self._fail(
                "realtime_invalid_event", "Client event sequence or session is invalid."
            )
            return False
        if isinstance(event, SessionConfigure):
            return await self._configure(event)
        if self._phase is ConnectionPhase.AWAITING_CONFIGURATION:
            await self._fail("realtime_invalid_event", "session.configure must be the first event.")
            return False
        if isinstance(event, InputAudioStart):
            if (
                self._phase is not ConnectionPhase.READY
                or event.payload.format != INPUT_AUDIO_FORMAT
            ):
                await self._fail("realtime_invalid_event", "Input audio cannot start now.")
                return False
            self._phase = ConnectionPhase.INPUT_OPEN
            self._open_stream_id = event.payload.stream_id
            self._open_stream_turn_id = event.turn_id
            self._received_audio_bytes = 0
            return True
        if isinstance(event, InputAudioCommit):
            if (
                self._phase is not ConnectionPhase.INPUT_OPEN
                or event.payload.stream_id != self._open_stream_id
            ):
                await self._fail("realtime_invalid_event", "No matching input stream is open.")
                return False
            expected_duration_ms = self._received_audio_bytes // 32
            if (
                event.payload.audio_bytes != self._received_audio_bytes
                or event.payload.duration_ms != expected_duration_ms
            ):
                await self._fail(
                    "realtime_invalid_event", "Input audio commit metadata does not match bytes."
                )
                return False
            stream_id = self._open_stream_id
            turn_id = event.turn_id or self._open_stream_turn_id or uuid5(stream_id, "turn")
            if not await self._queue_input(_CommitCommand(stream_id=stream_id, turn_id=turn_id)):
                return False
            self._open_stream_id = None
            self._open_stream_turn_id = None
            self._phase = ConnectionPhase.READY
            return True
        if isinstance(event, ResponseCancel):
            if (
                event.payload.reason == "barge_in"
                and "barge_in" not in self._configured_capabilities
            ):
                await self._fail(
                    "realtime_protocol_unsupported",
                    "Barge-in was not negotiated for this session.",
                )
                return False
            if event.response_id not in self._response_turns:
                await self._fail(
                    "realtime_invalid_event", "Response is not active on this connection."
                )
                return False
            self._cancelled_responses.add(event.response_id)
            for call_id, task in tuple(self._tool_tasks.items()):
                if self._tool_responses.get(call_id) == event.response_id:
                    task.cancel()
            return await self._queue_input(
                _CancelCommand(
                    response_id=event.response_id,
                    reason=event.payload.reason,
                )
            )
        if isinstance(event, Ping):
            await self._send_control("pong", {"nonce": event.payload.nonce})
            return True
        if isinstance(event, SessionClose):
            self._normal_close = True
            self._phase = ConnectionPhase.CLOSING
            return False
        await self._fail("realtime_invalid_event", "Unsupported realtime control event.")
        return False

    async def _handle_binary(self, data: bytes) -> bool:
        if self._phase is not ConnectionPhase.INPUT_OPEN:
            await self._fail("realtime_invalid_event", "Binary audio has no open input stream.")
            return False
        if not data or len(data) > self._max_binary_frame_bytes:
            await self._fail("realtime_invalid_event", "Binary audio frame size is invalid.")
            return False
        if not await self._queue_input(_AudioCommand(data=bytes(data))):
            return False
        self._received_audio_bytes += len(data)
        return True

    async def _input_worker(self) -> None:
        while True:
            command = await self._input_queue.get()
            try:
                try:
                    if isinstance(command, _AudioCommand):
                        await self._provider.send_audio(command.data)
                    elif isinstance(command, _CommitCommand):
                        await self._provider.commit_audio(
                            stream_id=command.stream_id, turn_id=command.turn_id
                        )
                    else:
                        await self._provider.cancel_response(command.response_id, command.reason)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._fail(
                        "realtime_provider_unavailable",
                        "Realtime provider is unavailable.",
                        retryable=True,
                        close_code=1011,
                    )
                    return
            finally:
                self._input_queue.task_done()

    async def _provider_reader(self) -> None:
        try:
            async for event in self._provider.events():
                await asyncio.wait_for(
                    self._output_queue.put(event),
                    timeout=self._backpressure_timeout,
                )
        except TimeoutError:
            await self._fail(
                "realtime_backpressure",
                "Realtime output queue is full.",
                retryable=True,
                close_code=1013,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail(
                "realtime_provider_unavailable",
                "Realtime provider is unavailable.",
                retryable=True,
                close_code=1011,
            )

    async def _run_tool(self, output: ProviderControl, call_id: UUID) -> None:
        assert output.turn_id is not None and output.response_id is not None
        name = str(output.payload.get("name", ""))
        arguments = output.payload.get("arguments")
        confirmation = output.payload.get("requires_confirmation")
        try:
            if self._tool_executor is None:
                payload: dict[str, object] = {
                    "call_id": str(call_id),
                    "name": name,
                    "status": "failed",
                    "error": {
                        "code": "tool_permission_denied",
                        "message": "Tool is not permitted.",
                    },
                }
                response: Mapping[str, object] = {"error": {"code": "tool_permission_denied"}}
            else:
                outcome = await self._tool_executor.execute(
                    principal=ToolPrincipal(self._session.user_id),
                    call_id=call_id,
                    turn_id=output.turn_id,
                    response_id=output.response_id,
                    name=name,
                    arguments=arguments,
                    proposed_confirmation=(
                        confirmation if isinstance(confirmation, bool) else None
                    ),
                )
                payload = outcome.event_payload()
                response = (
                    outcome.result
                    if outcome.result is not None
                    else {"error": {"code": outcome.error_code or "tool_execution_failed"}}
                )
            if output.response_id in self._cancelled_responses:
                return
            await self._send_control(
                "tool.call.completed",
                payload,
                turn_id=output.turn_id,
                response_id=output.response_id,
            )
            send = getattr(self._provider, "send_tool_result", None)
            if send is not None:
                send_result = cast(Callable[[UUID, Mapping[str, object]], Awaitable[None]], send)
                await send_result(call_id, response)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail(
                "realtime_provider_unavailable",
                "Tool execution is unavailable.",
                retryable=True,
                close_code=1011,
            )

    def _on_tool_done(self, call_id: UUID, _task: asyncio.Task[None]) -> None:
        self._tool_tasks.pop(call_id, None)
        self._tool_responses.pop(call_id, None)

    async def _output_worker(self) -> None:
        while True:
            output = await self._output_queue.get()
            try:
                if isinstance(output, ProviderAudio):
                    if output.response_id in self._cancelled_responses:
                        continue
                    async with self._send_lock:
                        self._last_server_send = time.monotonic()
                        await self._websocket.send_bytes(output.data)
                    continue
                if (
                    output.response_id in self._cancelled_responses
                    and output.event_type != "response.interrupted"
                ):
                    continue
                if output.event_type == "tool.call.completed":
                    # A provider cannot claim a tool succeeded without the policy executor.
                    continue
                if (
                    output.event_type == "response.started"
                    and output.response_id is not None
                    and output.turn_id is not None
                ):
                    self._response_turns[output.response_id] = output.turn_id
                await self._send_control(
                    output.event_type,
                    dict(output.payload),
                    turn_id=output.turn_id,
                    response_id=output.response_id,
                )
                if output.event_type == "tool.call.started":
                    if output.turn_id is None or output.response_id is None:
                        await self._fail("realtime_invalid_event", "Invalid tool call metadata.")
                        return
                    raw_call_id = output.payload.get("call_id")
                    try:
                        call_id = UUID(str(raw_call_id))
                    except (TypeError, ValueError):
                        await self._fail("realtime_invalid_event", "Invalid tool call metadata.")
                        return
                    if call_id in self._tool_tasks:
                        await self._fail("realtime_invalid_event", "Duplicate tool call.")
                        return
                    task = asyncio.create_task(self._run_tool(output, call_id))
                    self._tool_tasks[call_id] = task
                    self._tool_responses[call_id] = output.response_id
                    task.add_done_callback(partial(self._on_tool_done, call_id))
                if output.event_type == "response.interrupted" and output.response_id is not None:
                    self._response_turns.pop(output.response_id, None)
                elif output.event_type == "turn.completed" and output.turn_id is not None:
                    completed_ids = [
                        response_id
                        for response_id, turn_id in self._response_turns.items()
                        if turn_id == output.turn_id
                    ]
                    for response_id in completed_ids:
                        self._response_turns.pop(response_id, None)
            finally:
                self._output_queue.task_done()

    async def expire(self) -> None:
        """End a session that reached its maximum duration with a controlled error."""
        if self._failed or self._phase is ConnectionPhase.CLOSED:
            return
        logger.info("realtime_session_expired", extra={"session_id": self._session.id})
        METRICS.inc("mia_realtime_sessions_expired_total")
        await self._fail(
            "rate_limit_exceeded",
            "Voice session time limit reached.",
            retryable=True,
            close_code=1008,
        )

    async def run(self) -> None:
        input_task: asyncio.Task[None] | None = None
        provider_task: asyncio.Task[None] | None = None
        output_task: asyncio.Task[None] | None = None
        interrupted = True
        try:
            first = await self._websocket.receive()
            if first.get("type") == "websocket.disconnect":
                return
            if first.get("text") is None:
                await self._fail(
                    "realtime_invalid_event", "session.configure must be the first event."
                )
                return
            if not await self._handle_control(str(first["text"])):
                return
            input_task = asyncio.create_task(self._input_worker())
            provider_task = asyncio.create_task(self._provider_reader())
            output_task = asyncio.create_task(self._output_worker())

            while not self._failed and self._phase is not ConnectionPhase.CLOSING:
                waiting_since = time.monotonic()
                try:
                    message = await asyncio.wait_for(
                        self._websocket.receive(), timeout=_CLIENT_INACTIVITY_SECONDS
                    )
                except TimeoutError:
                    # Serving a response counts as traffic: a client waiting for the
                    # assistant to finish speaking legitimately sends nothing, and
                    # closing it then would cut off the answer.
                    if self._last_server_send > waiting_since:
                        continue
                    # Otherwise treat it as the disconnect it almost certainly is. No
                    # error event is sent: a vanished client cannot read one, and
                    # inventing a public error code would change the realtime schema
                    # without an ADR.
                    logger.info("realtime_client_inactive", extra={"session_id": self._session.id})
                    METRICS.inc("mia_realtime_sessions_client_inactive_total")
                    self._client_inactive = True
                    break
                if message.get("type") == "websocket.disconnect":
                    break
                text = message.get("text")
                data = message.get("bytes")
                if text is not None:
                    if not await self._handle_control(str(text)):
                        break
                elif data is not None:
                    if not await self._handle_binary(bytes(data)):
                        break
                else:
                    await self._fail("realtime_invalid_event", "Unsupported WebSocket frame.")
                    break
            interrupted = not self._normal_close
        except WebSocketDisconnect:
            interrupted = True
        finally:
            self._phase = ConnectionPhase.CLOSED
            await self._store.finish(self._session.id, interrupted=interrupted)
            await self._provider.close()
            for tool_task in tuple(self._tool_tasks.values()):
                tool_task.cancel()
            await asyncio.gather(*self._tool_tasks.values(), return_exceptions=True)
            for task in (input_task, provider_task, output_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (input_task, provider_task, output_task) if task is not None),
                return_exceptions=True,
            )
            if self._websocket.application_state is WebSocketState.CONNECTED:
                close_code = 1000 if self._normal_close else 1011
                if self._client_inactive:
                    close_code = 1001
                await self._websocket.close(code=close_code)


class RealtimeGateway:
    def __init__(
        self,
        *,
        store: InMemoryRealtimeSessionStore,
        authenticator: RealtimeAuthenticator,
        provider_factory: RealtimeProviderFactory,
        input_queue_size: int = 8,
        output_queue_size: int = 8,
        max_binary_frame_bytes: int = 4096,
        heartbeat_interval_ms: int = 15000,
        backpressure_timeout_seconds: float = 0.25,
        tool_executor: ToolExecutor | None = None,
        max_session_seconds: float = 900,
    ) -> None:
        if min(input_queue_size, output_queue_size, max_binary_frame_bytes) < 1:
            raise ValueError("realtime queue and frame limits must be positive")
        self._store = store
        self._authenticator = authenticator
        self._provider_factory = provider_factory
        self._input_queue_size = input_queue_size
        self._output_queue_size = output_queue_size
        self._max_binary_frame_bytes = max_binary_frame_bytes
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._backpressure_timeout_seconds = backpressure_timeout_seconds
        self._tool_executor = tool_executor
        self._max_session_seconds = max_session_seconds

    @staticmethod
    def _bearer_token(authorization: str | None) -> str | None:
        if authorization is None:
            return None
        scheme, separator, token = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and token:
            return token
        return None

    async def handle(self, websocket: WebSocket) -> None:
        token = self._bearer_token(websocket.headers.get("authorization"))
        principal = await self._authenticator.authenticate(token) if token else None
        raw_session_id = websocket.query_params.get("session_id")
        if principal is None or raw_session_id is None:
            await websocket.close(code=4401)
            return
        try:
            session_id = UUID(raw_session_id)
            session = await self._store.claim(session_id, principal.user_id)
        except (ValueError, RealtimeSessionNotFoundError):
            await websocket.close(code=4404)
            return
        await websocket.accept()
        request_id = websocket.scope.get("state", {}).get("request_id")
        provider = self._provider_factory(session)
        connection = _RealtimeConnection(
            websocket=websocket,
            session=session,
            provider=provider,
            store=self._store,
            input_queue_size=self._input_queue_size,
            output_queue_size=self._output_queue_size,
            max_binary_frame_bytes=self._max_binary_frame_bytes,
            heartbeat_interval_ms=self._heartbeat_interval_ms,
            backpressure_timeout_seconds=self._backpressure_timeout_seconds,
            tool_executor=self._tool_executor,
            request_id=request_id if isinstance(request_id, str) else None,
        )

        async def expire_after_limit() -> None:
            await asyncio.sleep(self._max_session_seconds)
            await connection.expire()

        watchdog = asyncio.create_task(expire_after_limit())
        try:
            # The watchdog closes the socket gracefully; the hard deadline only covers a
            # peer that never completes the close handshake.
            async with asyncio.timeout(self._max_session_seconds + _EXPIRY_GRACE_SECONDS):
                await connection.run()
        except TimeoutError:
            # ``run`` is cancelled and its finally block closes provider and claim.
            pass
        finally:
            watchdog.cancel()
