from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app.modules.conversations import (
    ConversationService,
    DeliveryStatus,
    EntryRole,
    TextSSEService,
    TurnStatus,
)
from app.modules.conversations.models import Entry, Turn
from app.modules.conversations.router import build_conversation_router
from app.modules.conversations.streaming import ConversationStreamEvent, PreparedTextTurn
from app.modules.realtime.sessions import AuthenticatedUser
from app.modules.users import UserRepository
from app.modules.users.models import User
from app.providers.ai.base import AIEvent, TextAIProvider, TextDelta, TextRequest
from app.providers.ai.fake import FakeAIProvider, FakeTextResponse
from fastapi import FastAPI, Request
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import Message, Scope

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests",
)

OWNER_ID = UUID("90000000-0000-4000-8000-000000000001")
STRANGER_ID = UUID("90000000-0000-4000-8000-000000000002")
OWNER_TOKEN = "owner-token"
STRANGER_TOKEN = "stranger-token"
STREAM_SCHEMA = json.loads(Path("contracts/openapi/v1/openapi.json").read_text())["components"][
    "schemas"
]["ConversationStreamEvent"]


class StaticAuthenticator:
    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None:
        user_ids = {OWNER_TOKEN: OWNER_ID, STRANGER_TOKEN: STRANGER_ID}
        user_id = user_ids.get(bearer_token)
        return AuthenticatedUser(user_id) if user_id else None


class CountingFakeProvider(FakeAIProvider):
    def __init__(self) -> None:
        super().__init__(FakeTextResponse(text_chunks=("Привет, ", "мир!")))
        self.calls = 0
        self.closed = 0
        self.requests: list[TextRequest] = []

    async def stream(self, request: TextRequest) -> AsyncIterator[AIEvent]:
        self.calls += 1
        self.requests.append(request)
        try:
            async for event in super().stream(request):
                yield event
        finally:
            self.closed += 1


class BlockingProvider(CountingFakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def stream(self, request: TextRequest) -> AsyncIterator[AIEvent]:
        try:
            async for event in super().stream(request):
                yield event
                if isinstance(event, TextDelta):
                    self.blocked.set()
                    await self.release.wait()
        finally:
            self.finished.set()


class RaisingProvider:
    async def stream(self, request: TextRequest) -> AsyncIterator[AIEvent]:
        if False:  # pragma: no cover - keeps this method an async generator
            yield cast(AIEvent, None)
        raise RuntimeError("provider details must not escape")


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert TEST_DATABASE_URL is not None
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    await asyncio.to_thread(command.upgrade, configuration, "head")
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await _new_user(session, OWNER_ID, "owner")
        await _new_user(session, STRANGER_ID, "stranger")
    yield factory
    await engine.dispose()


async def _new_user(session: AsyncSession, user_id: UUID, subject: str) -> User:
    user = await UserRepository(session).create(
        google_sub=subject,
        email=f"{subject}@example.test",
        email_verified=True,
        locale="ru-BY",
        timezone="Europe/Minsk",
    )
    user.id = user_id
    return user


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "X-Request-ID": "request-s14"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _build_app(factory: async_sessionmaker[AsyncSession], provider: TextAIProvider) -> FastAPI:
    application = FastAPI()

    @application.middleware("http")
    async def request_id(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = request.headers.get("X-Request-ID", "generated-request")
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    stream_service = TextSSEService(factory, provider)
    application.include_router(
        build_conversation_router(
            session_factory=factory,
            authenticator=StaticAuthenticator(),
            stream_service=stream_service,
        )
    )
    return application


def _sse_events(body: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for frame in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        data = cast(dict[str, object], json.loads(fields["data"]))
        events.append({"name": fields["event"], "id": fields["id"], "data": data})
    return events


@pytest.mark.asyncio
async def test_crud_pagination_and_auth_ownership(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _build_app(session_factory, CountingFakeProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthorized = await client.post(
            "/v1/conversations",
            headers={"Idempotency-Key": "conversation-key-unauthorized"},
            json={"title": "No auth"},
        )
        assert unauthorized.status_code == 401

        first = await client.post(
            "/v1/conversations",
            headers=_headers(OWNER_TOKEN, "conversation-create-key-001"),
            json={"title": "First"},
        )
        assert first.status_code == 201
        conversation_id = first.json()["id"]
        replay = await client.post(
            "/v1/conversations",
            headers=_headers(OWNER_TOKEN, "conversation-create-key-001"),
            json={"title": "First"},
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == conversation_id
        conflict = await client.post(
            "/v1/conversations",
            headers=_headers(OWNER_TOKEN, "conversation-create-key-001"),
            json={"title": "Changed"},
        )
        assert conflict.status_code == 409

        second = await client.post(
            "/v1/conversations",
            headers=_headers(OWNER_TOKEN, "conversation-create-key-002"),
            json={"title": "Second"},
        )
        assert second.status_code == 201
        page_one = await client.get("/v1/conversations?limit=1", headers=_headers(OWNER_TOKEN))
        assert page_one.status_code == 200
        cursor = page_one.json()["next_cursor"]
        assert cursor
        page_two = await client.get(
            f"/v1/conversations?limit=1&cursor={cursor}",
            headers=_headers(OWNER_TOKEN),
        )
        assert page_two.status_code == 200
        assert page_two.json()["items"]

        hidden = await client.get(
            f"/v1/conversations/{conversation_id}", headers=_headers(STRANGER_TOKEN)
        )
        assert hidden.status_code == 404
        updated = await client.patch(
            f"/v1/conversations/{conversation_id}",
            headers=_headers(OWNER_TOKEN, "conversation-patch-key-001"),
            json={"title": None, "archived": True},
        )
        assert updated.status_code == 200
        assert updated.json()["title"] is None
        assert updated.json()["archived_at"] is not None

        deleted = await client.delete(
            f"/v1/conversations/{conversation_id}",
            headers=_headers(OWNER_TOKEN, "conversation-delete-key-001"),
        )
        assert deleted.status_code == 204
        deleted_replay = await client.delete(
            f"/v1/conversations/{conversation_id}",
            headers=_headers(OWNER_TOKEN, "conversation-delete-key-001"),
        )
        assert deleted_replay.status_code == 204


@pytest.mark.asyncio
async def test_fake_provider_sse_persists_and_replays_without_duplicate_turn(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = CountingFakeProvider()
    app = _build_app(session_factory, provider)
    service = ConversationService(session_factory)
    conversation = await service.create_conversation(
        user_id=OWNER_ID,
        idempotency_key="conversation-for-stream-001",
        title="Text",
    )
    path = f"/v1/conversations/{conversation.value.id}/messages:stream"
    headers = _headers(OWNER_TOKEN, "message-stream-key-001")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        streamed = await client.post(path, headers=headers, json={"text": "Привет"})
        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(streamed.text)
        assert [event["name"] for event in events] == [
            "message.delta",
            "message.delta",
            "message.completed",
        ]
        assert cast(dict[str, object], events[-1]["data"])["type"] == "message.completed"
        validator = Draft202012Validator(STREAM_SCHEMA, format_checker=FormatChecker())
        for event in events:
            validator.validate(event["data"])

        replay = await client.post(path, headers=headers, json={"text": "Привет"})
        replay_events = _sse_events(replay.text)
        assert [event["name"] for event in replay_events] == ["message.completed"]
        replay_data = cast(dict[str, object], replay_events[0]["data"])
        replay_payload = cast(dict[str, object], replay_data["payload"])
        assert replay_payload["replayed"] is True
        assert provider.calls == 1

        conflict = await client.post(path, headers=headers, json={"text": "Другой текст"})
        assert conflict.status_code == 409

        entries_response = await client.get(
            f"/v1/conversations/{conversation.value.id}/entries?limit=1",
            headers=_headers(OWNER_TOKEN),
        )
        assert entries_response.status_code == 200
        assert entries_response.json()["next_cursor"]
        next_page = await client.get(
            f"/v1/conversations/{conversation.value.id}/entries",
            params={"limit": 1, "cursor": entries_response.json()["next_cursor"]},
            headers=_headers(OWNER_TOKEN),
        )
        assert next_page.json()["items"][0]["sequence"] == 2
        assert next_page.json()["next_cursor"] is None

    async with session_factory() as session:
        turns = tuple((await session.scalars(select(Turn))).all())
        entries = tuple((await session.scalars(select(Entry).order_by(Entry.sequence))).all())
    assert len(turns) == 1
    assert turns[0].status == TurnStatus.COMPLETED
    assert [(entry.role, entry.delivery_status) for entry in entries] == [
        (EntryRole.USER, DeliveryStatus.DELIVERED),
        (EntryRole.ASSISTANT, DeliveryStatus.DELIVERED),
    ]
    assert entries[1].content == {"text": "Привет, мир!"}


@pytest.mark.asyncio
async def test_disconnect_marks_interrupted_and_same_key_retries_in_place(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = CountingFakeProvider()
    service = TextSSEService(session_factory, provider)
    conversation = await ConversationService(session_factory).create_conversation(
        user_id=OWNER_ID,
        idempotency_key="conversation-for-disconnect",
    )
    prepared = await service.prepare(
        user_id=OWNER_ID,
        conversation_id=conversation.value.id,
        idempotency_key="message-disconnect-key-001",
        text="Привет",
    )
    stream = cast(
        AsyncGenerator[ConversationStreamEvent, None],
        service.stream(prepared, request_id="disconnect-request"),
    )
    first_event = await anext(stream)
    assert first_event.name == "message.delta"
    await stream.aclose()
    assert provider.closed == 1

    async with session_factory() as session:
        interrupted = await session.get(Turn, prepared.turn_id)
        assert interrupted is not None
        assert interrupted.status == TurnStatus.INTERRUPTED

    retry = await service.prepare(
        user_id=OWNER_ID,
        conversation_id=conversation.value.id,
        idempotency_key="message-disconnect-key-001",
        text="Привет",
    )
    retry_events = [event async for event in service.stream(retry, request_id="retry-request")]
    assert retry.turn_id == prepared.turn_id
    assert retry.assistant_entry_id == prepared.assistant_entry_id
    assert retry_events[-1].name == "message.completed"
    assert provider.calls == 2

    async with session_factory() as session:
        completed = await session.get(Turn, prepared.turn_id)
        entries = tuple((await session.scalars(select(Entry))).all())
    assert completed is not None
    assert completed.status == TurnStatus.COMPLETED
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_provider_exception_is_sanitized_and_marks_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = TextSSEService(session_factory, RaisingProvider())
    conversation = await ConversationService(session_factory).create_conversation(
        user_id=OWNER_ID,
        idempotency_key="conversation-for-failure-001",
    )
    prepared = await service.prepare(
        user_id=OWNER_ID,
        conversation_id=conversation.value.id,
        idempotency_key="message-failure-key-001",
        text="Привет",
    )
    events = [event async for event in service.stream(prepared, request_id="failed-request")]
    assert [event.name for event in events] == ["error"]
    assert events[0].data["payload"] == {
        "code": "internal_error",
        "message": "The response could not be completed.",
        "request_id": "failed-request",
        "retryable": True,
    }
    async with session_factory() as session:
        failed = await session.get(Turn, prepared.turn_id)
    assert failed is not None
    assert failed.status == TurnStatus.FAILED

    retry_service = TextSSEService(session_factory, CountingFakeProvider())
    retry = await retry_service.prepare(
        user_id=OWNER_ID,
        conversation_id=conversation.value.id,
        idempotency_key="message-failure-key-001",
        text="Привет",
    )
    assert retry.turn_id == prepared.turn_id
    assert retry.response_id != prepared.response_id
    # A previous HTTP response may finish cleaning up after the next request has started.
    await service.interrupt(prepared)
    assert [event.name async for event in retry_service.stream(retry, request_id="retry")][-1] == (
        "message.completed"
    )


@pytest.mark.parametrize(
    "method,suffix,payload",
    [
        ("GET", "", None),
        ("GET", "/entries", None),
        ("PATCH", "", {"title": "forbidden"}),
        ("DELETE", "", None),
        ("POST", "/messages:stream", {"text": "forbidden"}),
    ],
)
async def test_all_history_operations_hide_foreign_conversations(
    session_factory: async_sessionmaker[AsyncSession],
    method: str,
    suffix: str,
    payload: dict[str, str] | None,
) -> None:
    conversation = await ConversationService(session_factory).create_conversation(
        user_id=OWNER_ID, idempotency_key="ownership-create-key-001"
    )
    provider = CountingFakeProvider()
    app = _build_app(session_factory, provider)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(
            method,
            f"/v1/conversations/{conversation.value.id}{suffix}",
            headers=_headers(STRANGER_TOKEN, "ownership-mutation-key-001"),
            json=payload,
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    assert provider.calls == 0


async def test_context_excludes_failed_answers_and_later_turns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await ConversationService(session_factory).create_conversation(
        user_id=OWNER_ID, idempotency_key="context-create-key-001"
    )
    provider = CountingFakeProvider()
    service = TextSSEService(session_factory, provider)

    async def prepare(key: str, text: str) -> PreparedTextTurn:
        return await service.prepare(
            user_id=OWNER_ID,
            conversation_id=conversation.value.id,
            idempotency_key=key,
            text=text,
        )

    failed = await prepare("context-failed-key-001", "earlier user")
    partial_stream = cast(
        AsyncGenerator[ConversationStreamEvent, None],
        service.stream(failed, request_id="context-partial"),
    )
    await anext(partial_stream)
    await partial_stream.aclose()
    current = await prepare("context-current-key-001", "current user")
    await prepare("context-future-key-001", "future user")
    events = [event async for event in service.stream(current, request_id="context-current")]
    assert events[-1].name == "message.completed"
    assert [message.text for message in provider.requests[-1].messages] == [
        "earlier user",
        "current user",
    ]


async def test_validation_and_cursor_errors_use_safe_contract(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _build_app(session_factory, CountingFakeProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        invalid = await client.post(
            "/v1/conversations", headers=_headers(OWNER_TOKEN), json={"unknown": "private-input"}
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "validation_error"
        assert "private-input" not in invalid.text
        for cursor in ("%%%", "é"):
            response = await client.get(
                "/v1/conversations", params={"cursor": cursor}, headers=_headers(OWNER_TOKEN)
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "bad_request"
        for payload in ({}, {"archived": None}, {"archived": "true"}):
            response = await client.patch(
                f"/v1/conversations/{OWNER_ID}",
                headers=_headers(OWNER_TOKEN, "validation-mutation-key-001"),
                json=payload,
            )
            assert response.status_code == 422


async def test_concurrent_duplicate_preparation_claims_one_pending_turn(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.modules.conversations.streaming import TurnAlreadyStreamingError

    conversation = await ConversationService(session_factory).create_conversation(
        user_id=OWNER_ID, idempotency_key="concurrent-create-key-001"
    )
    service = TextSSEService(session_factory, CountingFakeProvider())

    async def prepare() -> object:
        return await service.prepare(
            user_id=OWNER_ID,
            conversation_id=conversation.value.id,
            idempotency_key="concurrent-stream-key-001",
            text="one request",
        )

    results = await asyncio.gather(prepare(), prepare(), return_exceptions=True)
    assert sum(isinstance(item, TurnAlreadyStreamingError) for item in results) == 1
    async with session_factory() as session:
        turns = list(await session.scalars(select(Turn)))
        entries = list(await session.scalars(select(Entry).order_by(Entry.sequence)))
    assert len(turns) == 1
    assert turns[0].status == TurnStatus.PENDING
    assert len(entries) == 2
    assert entries[1].delivery_status == DeliveryStatus.PENDING


def _stream_scope(path: str) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {OWNER_TOKEN}".encode()),
            (b"idempotency-key", b"transport-stream-key-001"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }


async def test_http_disconnect_cancels_stalled_provider_and_allows_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await ConversationService(session_factory).create_conversation(
        user_id=OWNER_ID, idempotency_key="transport-create-key-001"
    )
    provider = BlockingProvider()
    app = _build_app(session_factory, provider)
    path = f"/v1/conversations/{conversation.value.id}/messages:stream"
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b'{"text":"hello"}', "more_body": False})
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async with asyncio.timeout(10):
        task = asyncio.create_task(app(_stream_scope(path), incoming.get, send))
        await provider.blocked.wait()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            duplicate = await client.post(
                path,
                headers=_headers(OWNER_TOKEN, "transport-stream-key-001"),
                json={"text": "hello"},
            )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["retryable"] is True
        await incoming.put({"type": "http.disconnect"})
        await task
    assert provider.finished.is_set()
    async with session_factory() as session:
        turn = await session.scalar(select(Turn))
        entry = await session.scalar(select(Entry).where(Entry.role == EntryRole.ASSISTANT))
    assert turn is not None and turn.status == TurnStatus.INTERRUPTED
    assert entry is not None and entry.delivery_status == DeliveryStatus.INTERRUPTED
    assert any(message["type"] == "http.response.body" for message in sent)
    provider.release.set()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        retry = await client.post(
            path, headers=_headers(OWNER_TOKEN, "transport-stream-key-001"), json={"text": "hello"}
        )
    assert _sse_events(retry.text)[-1]["name"] == "message.completed"


async def test_transport_failure_before_body_releases_pending_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation = await ConversationService(session_factory).create_conversation(
        user_id=OWNER_ID, idempotency_key="early-disconnect-create-001"
    )
    provider = CountingFakeProvider()
    # No buffering middleware: fail the actual response-start send before body iteration.
    app = FastAPI()
    app.include_router(
        build_conversation_router(
            session_factory=session_factory,
            authenticator=StaticAuthenticator(),
            stream_service=TextSSEService(session_factory, provider),
        )
    )
    path = f"/v1/conversations/{conversation.value.id}/messages:stream"
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b'{"text":"hello"}', "more_body": False})

    async def send(message: Message) -> None:
        raise ConnectionError("client closed transport")

    async with asyncio.timeout(10):
        with pytest.raises(ConnectionError, match="client closed transport"):
            await app(_stream_scope(path), incoming.get, send)
    async with session_factory() as session:
        turn = await session.scalar(select(Turn))
    assert turn is not None and turn.status == TurnStatus.INTERRUPTED
    assert provider.calls == 0
