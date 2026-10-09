"""Shared harness for S32 tool journeys against the real composed application.

Only the model boundary is substituted: the text provider script, the realtime
provider script, or the Gemini Live wire transport. Authentication, routing,
admission, the tool registry and policy, the executor, the PostgreSQL audit,
canonical history and the public SSE/WebSocket envelopes are the real ones
built by ``app.main.create_app``/``app.composition.install_runtime_api``.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app import composition
from app.core.config import Settings
from app.core.runtime import Runtime
from app.main import create_app
from app.modules.auth.models import AuthSession, Device
from app.modules.auth.tokens import TokenManager
from app.modules.realtime.contracts import (
    INPUT_AUDIO_FORMAT,
    OUTPUT_AUDIO_FORMAT,
    ConfigurePayload,
)
from app.modules.realtime.provider import ProviderAudio, ProviderControl, ProviderOutput
from app.modules.realtime.sessions import RealtimeSession
from app.modules.tools import ToolExecutor, ToolRegistry, current_datetime_registry
from app.modules.tools.models import ToolExecution
from app.modules.tools.registry import CurrentDatetimeResult
from app.modules.users.models import User
from app.providers.ai.base import (
    AIEvent,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    TextRequest,
    ToolCallRequested,
    UsageReported,
)
from app.providers.ai.base.events import FinishReason, ToolRisk
from app.providers.ai.gemini_live import GeminiLiveAdapterFactory, GeminiLiveSetup
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.genai import types  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from pydantic import BaseModel, SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.testclient import WebSocketTestSession

DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("MIA_TEST_REDIS_URL")

requires_backend = pytest.mark.skipif(
    DATABASE_URL is None or REDIS_URL is None,
    reason="MIA_TEST_DATABASE_URL and MIA_TEST_REDIS_URL are required for tool E2E tests",
)

USER_ID = UUID("90000000-0000-4000-8000-0000000000e2")
DEVICE_ID = UUID("a0000000-0000-4000-8000-0000000000e2")
AUTH_SESSION_ID = UUID("b0000000-0000-4000-8000-0000000000e2")
FAMILY_ID = UUID("c0000000-0000-4000-8000-0000000000e2")
STREAM_ID = UUID("30000000-0000-4000-8000-0000000000e2")
TURN_ID = UUID("40000000-0000-4000-8000-0000000000e2")
TOOL_NAME = "system.get_current_datetime"
LOCALE = "ru-BY"
AT = "2026-09-19T10:00:00Z"
FROZEN_NOW = datetime(2026, 9, 19, 10, tzinfo=UTC)
FROZEN_LOCAL = "2026-09-19T13:00:00+03:00"
# A deterministic placeholder in a unique column; never a real token or hash.
REFRESH_TOKEN_HASH = "e2" * 32

_ROOT = Path(__file__).parents[2]


def database_url() -> str:
    assert DATABASE_URL is not None
    return DATABASE_URL


def redis_url() -> str:
    assert REDIS_URL is not None
    return REDIS_URL


# --------------------------------------------------------------------------- #
# Database preparation and audit inspection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AuditRow:
    """Detached snapshot of one ``tool_executions`` row."""

    user_id: UUID
    call_id: UUID
    turn_id: UUID | None
    response_id: UUID | None
    name: str
    status: str
    result: Mapping[str, Any] | None
    error_code: str | None


async def _seed() -> None:
    engine = create_async_engine(database_url())
    try:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE users CASCADE"))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        async with factory() as session, session.begin():
            session.add(
                User(
                    id=USER_ID,
                    google_sub="e2e-tools-user",
                    email="e2e-tools@example.test",
                    email_verified=True,
                    locale=LOCALE,
                    timezone="Europe/Minsk",
                )
            )
            await session.flush()
            session.add(
                Device(
                    id=DEVICE_ID,
                    user_id=USER_ID,
                    platform="android",
                    app_version="e2e-tools",
                    capabilities={},
                    last_seen_at=now,
                )
            )
            await session.flush()
            session.add(
                AuthSession(
                    id=AUTH_SESSION_ID,
                    family_id=FAMILY_ID,
                    user_id=USER_ID,
                    device_id=DEVICE_ID,
                    refresh_token_hash=REFRESH_TOKEN_HASH,
                    expires_at=now + timedelta(days=1),
                )
            )
    finally:
        await engine.dispose()


def reset_backend() -> None:
    """Migrate to head and leave exactly one authenticated user behind."""
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url())
    command.upgrade(configuration, "head")
    asyncio.run(_seed())


async def audit_rows() -> tuple[AuditRow, ...]:
    engine = create_async_engine(database_url())
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(ToolExecution).order_by(ToolExecution.created_at, ToolExecution.name)
                    )
                ).all()
            )
        return tuple(
            AuditRow(
                user_id=row.user_id,
                call_id=row.call_id,
                turn_id=row.turn_id,
                response_id=row.response_id,
                name=row.name,
                status=row.status.value,
                result=row.result,
                error_code=row.error_code,
            )
            for row in rows
        )
    finally:
        await engine.dispose()


def audit_rows_blocking() -> tuple[AuditRow, ...]:
    """Read the audit from a synchronous (WebSocket ``TestClient``) test."""
    return asyncio.run(audit_rows())


# --------------------------------------------------------------------------- #
# Composed application
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ComposedApp:
    app: FastAPI
    token: str
    settings: Settings
    executors: list[ToolExecutor]

    def headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def aclose(self) -> None:
        await cast(Runtime, self.app.state.runtime).close()


def compose(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ai_provider: Literal["fake", "gemini"] = "fake",
    text_provider: object | None = None,
    realtime_provider: Callable[[RealtimeSession], Any] | None = None,
    registry: ToolRegistry | None = None,
    live_transport_factory: object | None = None,
) -> ComposedApp:
    """Build the real application, substituting only the model boundary."""
    gemini = ai_provider == "gemini"
    settings = Settings(
        app_env="test",
        ai_provider=ai_provider,
        database_url=database_url(),
        redis_url=redis_url(),
        # Keep admission out of the way; quotas have their own S19 tests.
        max_daily_text_turns=10000,
        max_daily_live_sessions=1000,
        gemini_api_key=SecretStr("credential-used-only-by-fake") if gemini else None,
        gemini_text_model="e2e-text-model" if gemini else None,
        gemini_live_model="e2e-live-model" if gemini else None,
    )
    executors: list[ToolExecutor] = []

    def build_executor(
        session_factory: async_sessionmaker[AsyncSession], tool_registry: ToolRegistry
    ) -> ToolExecutor:
        executor = ToolExecutor(session_factory, tool_registry)
        executors.append(executor)
        return executor

    monkeypatch.setattr(composition, "ToolExecutor", build_executor)
    if registry is not None:
        monkeypatch.setattr(composition, "current_datetime_registry", lambda: registry)
    if text_provider is not None:
        name = "GeminiTextAIProvider" if gemini else "FakeAIProvider"
        monkeypatch.setattr(composition, name, lambda *_args, **_kwargs: text_provider)
    if realtime_provider is not None:
        monkeypatch.setattr(composition, "FakeRealtimeProvider", realtime_provider)
    if live_transport_factory is not None:
        monkeypatch.setattr(
            composition,
            "GeminiLiveAdapterFactory",
            lambda config: GeminiLiveAdapterFactory(
                config, transport_factory=cast(Any, live_transport_factory)
            ),
        )

    application = create_app(settings)
    tokens = TokenManager(
        settings.auth_jwt_secret.get_secret_value(),
        access_ttl_seconds=settings.auth_access_token_ttl_seconds,
    )
    token, _ = tokens.issue_access(USER_ID, FAMILY_ID)
    return ComposedApp(app=application, token=token, settings=settings, executors=executors)


# --------------------------------------------------------------------------- #
# Registries: the registered definition and limits stay; only the clock changes
# --------------------------------------------------------------------------- #

ToolHandler = Callable[[BaseModel], Awaitable[BaseModel]]


def frozen_registry() -> ToolRegistry:
    return current_datetime_registry(clock=lambda: FROZEN_NOW)


def registry_with_handler(handler: ToolHandler) -> ToolRegistry:
    """Keep the registered name, schema, risk and limits; replace the handler."""
    tool = current_datetime_registry().get(TOOL_NAME)
    assert tool is not None
    return ToolRegistry((replace(tool, handler=handler),))


def slow_handler(seconds: float) -> ToolHandler:
    async def handler(_arguments: BaseModel) -> BaseModel:
        await asyncio.sleep(seconds)
        return CurrentDatetimeResult(local_datetime=FROZEN_LOCAL, timezone="Europe/Minsk")

    return handler


def oversized_handler(size: int) -> ToolHandler:
    async def handler(_arguments: BaseModel) -> BaseModel:
        return CurrentDatetimeResult(local_datetime="x" * size, timezone="Europe/Minsk")

    return handler


def payload_handler(local_datetime: str) -> ToolHandler:
    async def handler(_arguments: BaseModel) -> BaseModel:
        return CurrentDatetimeResult(local_datetime=local_datetime, timezone="Europe/Minsk")

    return handler


def blocking_handler(entered: threading.Event | asyncio.Event) -> ToolHandler:
    async def handler(_arguments: BaseModel) -> BaseModel:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking tool handler must never return")

    return handler


# --------------------------------------------------------------------------- #
# Text modality: scripted model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ScriptedToolCall:
    name: str = TOOL_NAME
    arguments: Mapping[str, object] = field(default_factory=dict)
    call_id: UUID | None = None
    risk: ToolRisk = "read"
    requires_confirmation: bool = False


class ScriptedTextProvider:
    """Model stand-in: request at most one tool, then answer from its feedback."""

    def __init__(
        self,
        tool_call: ScriptedToolCall | None = None,
        *,
        opening: tuple[str, ...] = ("Секунду. ",),
        closing: tuple[str, ...] = ("Готово.",),
    ) -> None:
        self.tool_call = tool_call
        self._opening = opening
        self._closing = closing
        self.requests: list[TextRequest] = []

    async def stream(self, request: TextRequest) -> AsyncIterator[AIEvent]:
        self.requests.append(request)
        now = datetime.now(UTC)
        seq = 1

        def event_id(index: int) -> UUID:
            return uuid5(request.response_id, f"scripted-text:{index}")

        yield ResponseStarted(
            event_id=event_id(seq),
            turn_id=request.turn_id,
            response_id=request.response_id,
            seq=seq,
            at=now,
            modalities=("text",),
        )
        first_round = not request.tool_feedback
        for chunk in self._opening if first_round else self._closing:
            seq += 1
            yield TextDelta(
                event_id=event_id(seq),
                turn_id=request.turn_id,
                response_id=request.response_id,
                seq=seq,
                at=now,
                text=chunk,
            )
        finish_reason: FinishReason = "stop"
        if first_round and self.tool_call is not None:
            call = self.tool_call
            seq += 1
            yield ToolCallRequested(
                event_id=event_id(seq),
                turn_id=request.turn_id,
                response_id=request.response_id,
                seq=seq,
                at=now,
                call_id=call.call_id or uuid5(request.response_id, f"scripted-tool:{call.name}"),
                name=call.name,
                arguments=call.arguments,
                risk=call.risk,
                requires_confirmation=call.requires_confirmation,
            )
            finish_reason = "tool_result"
        seq += 1
        yield ResponseCompleted(
            event_id=event_id(seq),
            turn_id=request.turn_id,
            response_id=request.response_id,
            seq=seq,
            at=now,
            finish_reason=finish_reason,
        )
        seq += 1
        yield UsageReported(
            event_id=event_id(seq),
            turn_id=request.turn_id,
            response_id=request.response_id,
            seq=seq,
            at=now,
            provider_alias="scripted",
            model_alias="text-default",
            input_tokens=8,
            output_tokens=3,
        )


def stream_event_validator() -> Draft202012Validator:
    schema = json.loads((_ROOT / "contracts/openapi/v1/openapi.json").read_text())["components"][
        "schemas"
    ]["ConversationStreamEvent"]
    return Draft202012Validator(schema, format_checker=FormatChecker())


def sse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        events.append(
            {
                "name": fields["event"],
                "id": fields["id"],
                "data": cast(dict[str, Any], json.loads(fields["data"])),
            }
        )
    return events


def payload_of(event: Mapping[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], cast(dict[str, Any], event["data"])["payload"])


# --------------------------------------------------------------------------- #
# Realtime modality: scripted provider and scripted Gemini Live wire transport
# --------------------------------------------------------------------------- #

RealtimeScript = Callable[[UUID, UUID], Sequence[ProviderOutput]]


class ScriptedRealtimeProvider:
    """Replay a provider script and record explicit tool responses."""

    _CLOSED = object()

    def __init__(self, session: RealtimeSession, script: RealtimeScript) -> None:
        self.session = session
        self.tool_results: list[tuple[UUID, dict[str, object]]] = []
        self._script = script
        self._events: asyncio.Queue[ProviderOutput | object] = asyncio.Queue()
        self._configured = False
        self._closed = False
        self._cancelled: set[UUID] = set()
        self._response_turns: dict[UUID, UUID] = {}

    @staticmethod
    def response_id(turn_id: UUID) -> UUID:
        return uuid5(turn_id, "scripted-response")

    async def configure(self, config: ConfigurePayload) -> None:
        if config.conversation_id != self.session.conversation_id:
            raise ValueError("conversation does not match the realtime session")
        self._configured = True

    async def send_audio(self, chunk: bytes) -> None:
        if not self._configured or self._closed:
            raise RuntimeError("scripted provider is not ready")

    async def commit_audio(self, *, stream_id: UUID, turn_id: UUID) -> None:
        if not self._configured or self._closed:
            raise RuntimeError("scripted provider is not ready")
        response_id = self.response_id(turn_id)
        self._response_turns[response_id] = turn_id
        for event in self._script(turn_id, response_id):
            await self._events.put(event)

    async def cancel_response(self, response_id: UUID, reason: str) -> None:
        self._cancelled.add(response_id)
        await self._events.put(
            ProviderControl(
                "response.interrupted",
                {
                    "reason": "barge_in" if reason == "barge_in" else "user_request",
                    "delivered_audio_ms": 0,
                },
                turn_id=self._response_turns.get(response_id),
                response_id=response_id,
            )
        )

    async def send_tool_result(self, call_id: UUID, response: Mapping[str, object]) -> None:
        self.tool_results.append((call_id, dict(response)))

    async def events(self) -> AsyncIterator[ProviderOutput]:
        while True:
            event = await self._events.get()
            if event is self._CLOSED:
                return
            if isinstance(event, ProviderControl | ProviderAudio):
                yield event

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._events.put(self._CLOSED)


class ScriptedRealtimeFactory:
    def __init__(self, script: RealtimeScript) -> None:
        self._script = script
        self.sessions: list[ScriptedRealtimeProvider] = []

    def __call__(self, session: RealtimeSession) -> ScriptedRealtimeProvider:
        provider = ScriptedRealtimeProvider(session, self._script)
        self.sessions.append(provider)
        return provider


class ScriptedLiveTransport:
    """Gemini Live wire stand-in that answers a committed turn with a tool call."""

    _CLOSED = object()

    def __init__(self, on_commit: tuple[types.LiveServerMessage, ...]) -> None:
        self.tool_responses: list[tuple[str, str, dict[str, object]]] = []
        self.closed = False
        self._on_commit = on_commit
        self._incoming: asyncio.Queue[types.LiveServerMessage | object] = asyncio.Queue()

    async def send_audio(self, chunk: bytes) -> None:
        return None

    async def start_activity(self) -> None:
        return None

    async def end_activity(self) -> None:
        # Automatic activity detection is disabled, so the turn ends here.
        for message in self._on_commit:
            await self._incoming.put(message)

    async def send_tool_response(
        self, *, call_id: str, name: str, response: Mapping[str, object]
    ) -> None:
        self.tool_responses.append((call_id, name, dict(response)))

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        while True:
            message = await self._incoming.get()
            if message is self._CLOSED:
                return
            if isinstance(message, types.LiveServerMessage):
                yield message

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._incoming.put(self._CLOSED)


class ScriptedLiveTransportFactory:
    def __init__(self, on_commit: tuple[types.LiveServerMessage, ...]) -> None:
        self._on_commit = on_commit
        self.setups: list[GeminiLiveSetup] = []
        self.transports: list[ScriptedLiveTransport] = []

    async def connect(self, setup: GeminiLiveSetup) -> ScriptedLiveTransport:
        self.setups.append(setup)
        transport = ScriptedLiveTransport(self._on_commit)
        self.transports.append(transport)
        return transport


# --------------------------------------------------------------------------- #
# Realtime client protocol helpers
# --------------------------------------------------------------------------- #


def create_realtime_session(
    client: TestClient, composed: ComposedApp, conversation_id: UUID, *, key: str
) -> str:
    response = client.post(
        "/v1/realtime/sessions",
        headers=composed.headers(key),
        json={
            "conversation_id": str(conversation_id),
            "locale": LOCALE,
            "input_audio": INPUT_AUDIO_FORMAT.model_dump(mode="json"),
            "output_audio": OUTPUT_AUDIO_FORMAT.model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _client_event(
    session_id: str, event_type: str, seq: int, payload: dict[str, object], **extra: str
) -> dict[str, object]:
    event: dict[str, object] = {
        "v": 1,
        "type": event_type,
        "session_id": session_id,
        "seq": seq,
        "at": AT,
        "payload": payload,
    }
    event.update(extra)
    return event


def configure_event(session_id: str, conversation_id: UUID, *, seq: int = 1) -> dict[str, object]:
    return _client_event(
        session_id,
        "session.configure",
        seq,
        {
            "protocol_version": 1,
            "conversation_id": str(conversation_id),
            "locale": LOCALE,
            "input_audio": [INPUT_AUDIO_FORMAT.model_dump(mode="json")],
            "output_audio": [OUTPUT_AUDIO_FORMAT.model_dump(mode="json")],
            "capabilities": ["barge_in", "input_transcript", "output_transcript"],
        },
    )


def start_input_event(session_id: str, *, seq: int = 2) -> dict[str, object]:
    return _client_event(
        session_id,
        "input_audio.start",
        seq,
        {"stream_id": str(STREAM_ID), "format": INPUT_AUDIO_FORMAT.model_dump(mode="json")},
    )


def commit_input_event(session_id: str, *, seq: int = 3) -> dict[str, object]:
    return _client_event(
        session_id,
        "input_audio.commit",
        seq,
        {"stream_id": str(STREAM_ID), "audio_bytes": 1280, "duration_ms": 40},
        turn_id=str(TURN_ID),
    )


def cancel_event(session_id: str, response_id: UUID, *, seq: int) -> dict[str, object]:
    return _client_event(
        session_id,
        "response.cancel",
        seq,
        {"reason": "barge_in"},
        turn_id=str(TURN_ID),
        response_id=str(response_id),
    )


def close_event(session_id: str, *, seq: int) -> dict[str, object]:
    return _client_event(session_id, "session.close", seq, {"reason": "completed"})


def receive_frame(websocket: WebSocketTestSession) -> dict[str, Any] | bytes:
    message = websocket.receive()
    if message.get("bytes") is not None:
        return bytes(message["bytes"])
    return cast(dict[str, Any], json.loads(str(message["text"])))


def drain_until(
    websocket: WebSocketTestSession, event_type: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    """Collect control frames up to and including ``event_type``."""
    frames: list[dict[str, Any]] = []
    for _ in range(limit):
        frame = receive_frame(websocket)
        if isinstance(frame, bytes):
            continue
        frames.append(frame)
        if frame["type"] == event_type:
            return frames
    raise AssertionError(f"{event_type} was never received: {[f['type'] for f in frames]}")


def new_conversation(client: TestClient, composed: ComposedApp, *, key: str) -> UUID:
    response = client.post(
        "/v1/conversations", headers=composed.headers(key), json={"title": "E2E tools"}
    )
    assert response.status_code == 201, response.text
    return UUID(str(response.json()["id"]))


def fresh_key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


# --------------------------------------------------------------------------- #
# Text modality: HTTP helpers
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def text_client(composed: ComposedApp) -> AsyncIterator[httpx.AsyncClient]:
    """Drive the composed ASGI app over HTTP and release its runtime afterwards."""
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=composed.app), base_url="http://test"
        ) as client:
            yield client
    finally:
        await composed.aclose()


async def create_conversation(
    client: httpx.AsyncClient, composed: ComposedApp, *, key: str
) -> UUID:
    response = await client.post(
        "/v1/conversations", headers=composed.headers(key), json={"title": "E2E tools"}
    )
    assert response.status_code == 201, response.text
    return UUID(str(response.json()["id"]))


async def stream_turn(
    client: httpx.AsyncClient,
    composed: ComposedApp,
    conversation_id: UUID,
    *,
    key: str,
    text_body: str = "Который час?",
) -> httpx.Response:
    return await client.post(
        f"/v1/conversations/{conversation_id}/messages:stream",
        headers=composed.headers(key),
        json={"text": text_body},
    )


async def entries(
    client: httpx.AsyncClient, composed: ComposedApp, conversation_id: UUID
) -> list[dict[str, Any]]:
    response = await client.get(
        f"/v1/conversations/{conversation_id}/entries", headers=composed.headers()
    )
    assert response.status_code == 200, response.text
    return cast(list[dict[str, Any]], response.json()["items"])
