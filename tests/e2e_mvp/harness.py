"""M60 acceptance harness: the production composition with fake outside worlds only.

``create_app(settings)`` runs ``install_runtime_api`` exactly as the server does (auth, models,
store, tools, memory, runs, jobs, chats, Telegram, OAuth AS, device tools) on a real
PostgreSQL and Redis. Only what lives outside Mia is replaced:

- Google ID tokens: ``google:<sub>`` (``TokenGoogleVerifier``);
- the model: :class:`ScriptedModel` (no live model offline); it answers chat-title requests
  by itself and replays scripted steps for everything else;
- remote MCP servers and their authorization server: ``tests.catalog.remote_world``
  (Linear's recorded tool set by default), reached through the real ``McpConnector`` and
  ``OAuthClient`` with an injected resolver/transport;
- FCM: ``tests.jobs.conftest.FakeFcm``; the Telegram Bot API: :class:`FakeBotApi`;
- the phone: HTTP calls an Android client makes (bearer session, device tools, results,
  approvals) plus an in-process view of the run events (``httpx.ASGITransport`` buffers SSE).

The worker is ``app.worker.main.Worker`` over ``app.state.jobs`` driven tick by tick; a
"restart" builds a second application on the same database.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from app.core.config import Settings
from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelGatewayError,
    ModelInfo,
    ModelRequest,
    ModelToolCall,
    TextDelta,
    ToolCallRequested,
)
from app.core.ports.types import DEFAULT_MODEL, JsonObject
from app.main import create_app
from app.modules.auth.models import AuthSession
from app.modules.connections.oauth.client import OAuthClient
from app.modules.notifications.telegram_api import InlineButton, SendResult
from app.modules.plugins.builtin import BUILTIN_ROOT
from app.modules.plugins.catalog.package import load_package
from app.modules.plugins.catalog.service import PublishResult
from app.modules.plugins.catalog.showcase import showcase_for
from app.modules.tools.mcp_client import McpConnector
from app.modules.users import UserRepository
from app.worker.main import Worker
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.catalog import remote_world
from tests.catalog.remote_world import (
    MCP_HOST,
    RESOURCE,
    AuthorizationServer,
    FakeResolver,
    RemoteWorld,
    browser_authorize,
    running,
    transport_factory,
)
from tests.fakes import assert_mvp_valid
from tests.jobs.conftest import FakeFcm
from tests.workspace.harness import CLIENT_ID, TokenGoogleVerifier, migrate_to_head

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
TEST_REDIS_URL = os.environ.get("MIA_TEST_REDIS_URL")
requires_stack = pytest.mark.skipif(
    TEST_DATABASE_URL is None or TEST_REDIS_URL is None,
    reason="MIA_TEST_DATABASE_URL and MIA_TEST_REDIS_URL are required for the MVP acceptance",
)

PUBLIC_BASE = "https://mia.test.example"
ADMIN_SUB = "mvp-store-admin"
BOT_TOKEN = "123456:mvp-acceptance-fake-bot-token"  # not a secret: no Bot API is reached
WEBHOOK_SECRET = "mvp-acceptance-webhook-secret"
ROOT = Path(__file__).resolve().parents[2]
LINEAR_DIR = ROOT / "plugins" / "linear"
TERMINAL = {"run.completed", "run.failed", "run.cancelled"}
TITLE_PROMPT = "Придумай короткое название"

_keys = 0


def idem() -> dict[str, str]:
    global _keys
    _keys += 1
    return {"Idempotency-Key": f"mvp-acceptance-{uuid.uuid4().hex}-{_keys:06d}"}


def answer(speech: str, display: Any = None, followups: Sequence[str] = ()) -> str:
    """The structured final answer (ADR: ``{speech, display, followups}``)."""
    return json.dumps(
        {"speech": speech, "display": display, "followups": list(followups)}, ensure_ascii=False
    )


def say(speech: str, display: Any = None, followups: Sequence[str] = ()) -> list[ModelEvent]:
    return [TextDelta(answer(speech, display, followups)), ModelDone("stop")]


def call(name: str, arguments: JsonObject, call_id: str | None = None) -> list[ModelEvent]:
    tool = ModelToolCall(
        call_id=call_id or f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=arguments
    )
    return [ToolCallRequested(tool), ModelDone("tool_calls")]


Step = Sequence[ModelEvent] | Callable[[ModelRequest], Sequence[ModelEvent]] | ModelGatewayError


class ScriptedModel:
    """The primary model, scripted per test: each ``stream`` replays the next step.

    Chat titling (M74) is answered without consuming a step. A step may be a callable that
    sees the request (e.g. to answer with the tool name the router actually offered).
    """

    def __init__(self) -> None:
        self.info = ModelInfo(
            ref="mvp/scripted",
            provider="mvp",
            supports_tools=True,
            supports_structured_output=True,
            verified=True,
        )
        self.steps: deque[Step] = deque()
        self.requests: list[ModelRequest] = []
        self.titles = 0

    def then(self, *steps: Step) -> ScriptedModel:
        self.steps.extend(steps)
        return self

    @property
    def pending(self) -> int:
        return len(self.steps)

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        return self.info

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        first = request.messages[0].content or "" if request.messages else ""
        if first.startswith(TITLE_PROMPT):
            self.titles += 1
            yield TextDelta("Тестовый чат")
            yield ModelDone("stop")
            return
        self.requests.append(request)
        if not self.steps:
            raise ModelGatewayError("invalid_request", "MVP acceptance script is exhausted")
        step = self.steps.popleft()
        if isinstance(step, ModelGatewayError):
            raise step
        events = step(request) if callable(step) else step
        for event in events:
            yield event

    def offered(self, index: int = -1) -> set[str]:
        """Tool names the router offered in a model request."""
        return {tool.name for tool in self.requests[index].tools}

    def tool_messages(self, index: int = -1) -> list[str]:
        return [m.content or "" for m in self.requests[index].messages if m.role == "tool"]

    def system_text(self, index: int = -1) -> str:
        return "\n".join(
            m.content or "" for m in self.requests[index].messages if m.role == "system"
        )


class FakeBotApi:
    """Telegram Bot API without a network; replaces ``HttpTelegramBotApi``."""

    instances: list[FakeBotApi] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.sent: list[dict[str, Any]] = []
        FakeBotApi.instances.append(self)

    async def send_message(
        self, chat_id: int, text: str, buttons: Sequence[InlineButton] | None = None
    ) -> SendResult:
        self.sent.append(
            {"chat_id": chat_id, "text": text, "buttons": [b.text for b in buttons or []]}
        )
        return SendResult("sent")

    async def answer_callback(self, callback_id: str) -> None:
        return None

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": [], "edited": True})


@dataclass
class Person:
    """One signed-in client: an Android app (bearer) or a browser (cookie jar)."""

    mvp: Mvp
    subject: str
    headers: dict[str, str]
    user_id: str
    device_id: str | None = None
    client: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        return self.client or self.mvp.client

    def _headers(
        self, extra: dict[str, str] | None = None, *, mutate: bool = False
    ) -> dict[str, str]:
        out = dict(self.headers)
        if mutate:
            out.update(idem())
            if self.client is not None:  # browser: same-origin fetch
                out.update({"Origin": PUBLIC_BASE, "Sec-Fetch-Site": "same-origin"})
        out.update(extra or {})
        return out

    async def get(self, path: str, **params: Any) -> httpx.Response:
        return await self.http.get(path, headers=self._headers(), params=params or None)

    async def post(self, path: str, body: Any = None, **extra: str) -> httpx.Response:
        return await self.http.post(
            path, headers=self._headers(extra, mutate=True), json=body if body is not None else {}
        )

    async def put(self, path: str, body: Any) -> httpx.Response:
        return await self.http.put(path, headers=self._headers(mutate=True), json=body)

    async def patch(self, path: str, body: Any) -> httpx.Response:
        return await self.http.patch(path, headers=self._headers(mutate=True), json=body)

    async def delete(self, path: str) -> httpx.Response:
        return await self.http.delete(path, headers=self._headers(mutate=True))

    # -- the phone ------------------------------------------------------------------

    async def register_device_tools(self) -> None:
        """What the Android app sends after sign-in (M23 fixture, this device's id)."""
        assert self.device_id is not None, "only the phone registers device tools"
        body = json.loads(
            (ROOT / "tests/device_tools/fixtures/android_registration.json").read_text("utf-8")
        )
        body["device_id"] = self.device_id
        response = await self.put(f"/v1/devices/{self.device_id}/tools", body)
        assert response.status_code == 204, response.text

    async def push_token(self, token: str = "fcm-token-phone") -> None:
        assert self.device_id is not None
        response = await self.post(
            "/v1/devices/push-token",
            {"device_id": self.device_id, "provider": "fcm", "token": token},
        )
        assert response.status_code in (200, 201, 204), response.text

    async def answer_device_call(
        self, run_id: str, call_id: str, content: str, structured: JsonObject | None = None
    ) -> httpx.Response:
        body: dict[str, Any] = {"call_id": call_id, "status": "succeeded", "content": content}
        if structured is not None:
            body["structured_content"] = structured
        return await self.post(f"/v1/runs/{run_id}/device-tool-results", body)

    # -- runs -----------------------------------------------------------------------

    async def start_run(
        self,
        text_value: str,
        *,
        profile: str = "voice",
        on_device: bool = True,
        conversation_id: str | None = None,
        incognito: bool = False,
    ) -> str:
        body: dict[str, Any] = {"profile": profile, "input": {"text": text_value}}
        if on_device and self.device_id is not None:
            body["device_id"] = self.device_id
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        if incognito:
            body["incognito"] = True
        response = await self.post("/v1/runs", body)
        assert response.status_code == 202, response.text
        return str(response.json()["id"])

    async def events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        """All events until the terminal one, over the in-process stream, contract-checked."""
        return await self.mvp.collect(self.user_id, run_id, after=after)

    async def run(self, text_value: str, **options: Any) -> list[dict[str, Any]]:
        run_id = await self.start_run(text_value, **options)
        return await self.events(run_id)

    async def sse(self, run_id: str) -> list[dict[str, Any]]:
        """The same events over HTTP SSE (after the run ended: the transport buffers)."""
        response = await self.get(f"/v1/runs/{run_id}/events")
        assert response.status_code == 200, response.text
        return parse_sse(response.text)

    async def pending_approval(self, wait_s: float = 5.0) -> dict[str, Any]:
        """Poll ``GET /v1/approvals`` like the app's confirmation card does."""
        async with asyncio.timeout(wait_s):
            while True:
                listed = await self.get("/v1/approvals")
                assert listed.status_code == 200, listed.text
                items = [a for a in listed.json()["items"] if a["status"] == "pending"]
                if items:
                    return cast(dict[str, Any], items[0])
                await asyncio.sleep(0.02)

    async def decide(self, approval: dict[str, Any], decision: str = "approve") -> httpx.Response:
        return await self.post(
            f"/v1/approvals/{approval['id']}/decision",
            {"decision": decision, "args_hash": approval["args_hash"]},
        )


def parse_sse(body: str) -> list[dict[str, Any]]:
    frames = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if line and not line.startswith(":")
        )
        if "data" not in fields:
            continue
        data = json.loads(fields["data"])
        assert_mvp_valid(data, "run-events")
        frames.append(data)
    return frames


def types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def payloads(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in events if event["type"] == kind]


def final(events: list[dict[str, Any]]) -> dict[str, Any]:
    [response] = payloads(events, "response.final")
    return cast(dict[str, Any], response["response"])


@dataclass
class Mvp:
    app: FastAPI
    client: httpx.AsyncClient
    model: ScriptedModel
    fcm: FakeFcm
    remote: RemoteWorld
    auth_server: AuthorizationServer
    resolver: FakeResolver
    admin_id: str
    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    stack: AsyncExitStack
    people: list[Person] = field(default_factory=list)

    # -- duck-typed like tests.catalog.harness.Store for the shared helpers -------------

    @property
    def store(self) -> Any:
        return self.app.state.store

    @property
    def catalog(self) -> Any:
        return self.app.state.store.catalog

    @property
    def bot(self) -> FakeBotApi:
        return FakeBotApi.instances[-1]

    @property
    def runs(self) -> Any:
        return self.app.state.runs

    @property
    def jobs(self) -> Any:
        return self.app.state.jobs

    async def post(self, headers: dict[str, str], path: str, body: Any = None) -> httpx.Response:
        return await self.client.post(path, headers={**headers, **idem()}, json=body or {})

    async def get(self, headers: dict[str, str], path: str, **params: Any) -> httpx.Response:
        return await self.client.get(path, headers=headers, params=params or None)

    async def delete(self, headers: dict[str, str], path: str) -> httpx.Response:
        return await self.client.delete(path, headers={**headers, **idem()})

    # -- people -----------------------------------------------------------------------

    async def phone(self, subject: str) -> Person:
        """Sign in like the Android app (Google ID token → bearer session + device)."""
        nonce = await self.client.post("/v1/auth/google/nonce", headers=idem())
        assert nonce.status_code == 201, nonce.text
        login = await self.client.post(
            "/v1/auth/google",
            headers=idem(),
            json={
                "id_token": f"google:{subject}",
                "nonce": nonce.json()["nonce"],
                "device": {"platform": "android", "app_version": "0.1.0"},
            },
        )
        assert login.status_code == 200, login.text
        body = login.json()
        user_id = str(body["user"]["id"])
        async with self.factory() as session:
            device = await session.scalar(
                select(AuthSession.device_id)
                .where(AuthSession.user_id == uuid.UUID(user_id))
                .order_by(AuthSession.created_at.desc())
                .limit(1)
            )
        person = Person(
            self,
            subject,
            {"Authorization": f"Bearer {body['access_token']}"},
            user_id,
            device_id=str(device),
        )
        self.people.append(person)
        return person

    async def browser(self, subject: str) -> Person:
        """Sign in like the web app (Google Identity Services credential → cookie session)."""
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=PUBLIC_BASE
        )
        await self.stack.enter_async_context(client)
        browser_headers = {"Origin": PUBLIC_BASE, "Sec-Fetch-Site": "same-origin"}
        nonce = await client.post("/v1/auth/web/nonce", headers={**idem(), **browser_headers})
        assert nonce.status_code == 201, nonce.text
        login = await client.post(
            "/v1/auth/web/google",
            headers={**idem(), **browser_headers},
            json={"credential": f"google:{subject}"},
        )
        assert login.status_code == 200, login.text
        person = Person(self, subject, {}, str(login.json()["user"]["id"]), client=client)
        self.people.append(person)
        return person

    # -- store ------------------------------------------------------------------------

    async def publish_builtin(self, name: str) -> PublishResult:
        package = load_package(BUILTIN_ROOT / name)
        return cast(
            PublishResult,
            await self.catalog.publish(
                package, actor_user_id=self.admin_id, store_meta=showcase_for(package.plugin_id)
            ),
        )

    def linear_directory(self, tmp_path: Path) -> Path:
        """The real Linear package with only the server address pointed at the test server."""
        target = tmp_path / "linear"
        if not target.exists():
            shutil.copytree(LINEAR_DIR, target)
            manifest = json.loads((target / "mia.json").read_text("utf-8"))
            manifest["server"]["url"] = RESOURCE
            manifest["permissions"]["network"] = [MCP_HOST]
            (target / "mia.json").write_text(json.dumps(manifest), "utf-8")
        return target

    async def install(self, person: Person, plugin_id: str) -> dict[str, Any]:
        """«Установить» after the consent screen: the release's digest and grants."""
        entry = await person.get(f"/v1/plugins/{plugin_id}")
        assert entry.status_code == 200, entry.text
        consent = entry.json()["consent"]
        release = await self.catalog.release(plugin_id, None)
        response = await person.post(
            f"/v1/plugins/{plugin_id}/install",
            {
                "version": release.version,
                "tools_digest": consent.get("tools_digest", release.tools_digest),
                "grants": release.grants,
            },
        )
        assert response.status_code == 201, response.text
        return cast(dict[str, Any], response.json())

    async def connect(self, person: Person, installation_id: str) -> httpx.Response:
        """«Подключить аккаунт»: start the flow, consent at the provider, hit the callback."""
        started = await person.post(
            "/v1/connections", {"installation_id": installation_id, "return_to": "/store"}
        )
        assert started.status_code == 201, started.text
        location = await browser_authorize(self.remote, started.json()["authorization_url"])
        parts = httpx.URL(location)
        assert (
            f"{parts.scheme}://{parts.host}{parts.path}" == f"{PUBLIC_BASE}/v1/connections/callback"
        )
        return await person.http.get(f"{parts.path}?{parts.query.decode()}")

    # -- runs and the worker --------------------------------------------------------------

    async def collect(
        self, user_id: str, run_id: str, *, after: int = 0, wait_s: float = 15.0
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async with asyncio.timeout(wait_s):
            async for event in self.runs.events(
                user_id=uuid.UUID(user_id), run_id=uuid.UUID(run_id), after_sequence=after
            ):
                if event is None:
                    continue
                wire = event.to_wire()
                if not (
                    wire["type"] == "tool.call.started"
                    and "." not in wire["payload"].get("tool", ".")
                ):
                    # OI-M74-R1-002: core ``notify`` is not namespaced; everything else is checked.
                    assert_mvp_valid(wire, "run-events")
                events.append(wire)
                if wire["type"] in TERMINAL:
                    break
        await self.runs.wait_finished(uuid.UUID(run_id))
        return events

    async def wait_event(
        self, user_id: str, run_id: str, kind: str, wait_s: float = 10.0
    ) -> dict[str, Any]:
        """The first event of ``kind`` while the run is still going (e.g. a device call)."""
        async with asyncio.timeout(wait_s):
            async for event in self.runs.events(
                user_id=uuid.UUID(user_id), run_id=uuid.UUID(run_id), after_sequence=0
            ):
                if event is None:
                    continue
                wire = event.to_wire()
                if wire["type"] == kind:
                    return cast(dict[str, Any], wire)
                if wire["type"] in TERMINAL:
                    raise AssertionError(f"run ended before {kind}: {wire['type']}")
        raise AssertionError("unreachable")

    def worker(self, name: str = "worker-1") -> Worker:
        return Worker(self.jobs, worker_id=name)

    async def sql(self, statement: str, **params: Any) -> list[Any]:
        async with self.factory() as session, session.begin():
            result = await session.execute(text(statement), params)
            return list(result.all()) if cast(Any, result).returns_rows else []


class _RealClock:
    """Wall-clock seconds for the provider's authorization server (Mia uses ``time.time``)."""

    def __call__(self) -> float:
        return time.time()

    @property
    def now(self) -> float:
        return time.time()


def _settings(admin_id: str) -> Settings:
    return Settings(
        app_env="test",
        log_level="WARNING",
        database_url=cast(str, TEST_DATABASE_URL),
        redis_url=cast(str, TEST_REDIS_URL),
        google_client_id=CLIENT_ID,
        public_base_url=PUBLIC_BASE,
        store_admin_user_ids=admin_id,
        tools_list_cache_seconds=0,
        tools_call_timeout_seconds=10,
        auth_nonce_rate_limit=1000,
        auth_exchange_rate_limit=1000,
        auth_authenticated_rate_limit=5000,
        chatgpt_plan_loopback_listener=False,
        telegram_bot_token=BOT_TOKEN,
        telegram_bot_username="mia_mvp_bot",
        telegram_webhook_secret=WEBHOOK_SECRET,
    )


async def reset_database() -> str:
    """Migrate to head, empty every table, create the store admin; return the admin id."""
    await asyncio.to_thread(migrate_to_head)
    engine = create_async_engine(cast(str, TEST_DATABASE_URL))
    try:
        async with engine.begin() as connection:
            tables = (
                (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                            "AND tablename <> 'alembic_version'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            if tables:
                await connection.execute(
                    text("TRUNCATE TABLE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
                )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            admin = await UserRepository(session).create(
                google_sub=ADMIN_SUB,
                email="admin@example.test",
                email_verified=True,
                locale="ru-BY",
                timezone="Europe/Minsk",
            )
        return str(admin.id)
    finally:
        await engine.dispose()


def patch_outside_world(
    monkeypatch: pytest.MonkeyPatch,
    model: ScriptedModel,
    fcm: FakeFcm,
    world: RemoteWorld,
    resolver: FakeResolver,
) -> None:
    import app.modules.auth.composition as auth_composition  # noqa: PLC0415
    import app.modules.jobs.composition as jobs_composition  # noqa: PLC0415
    import app.modules.models.composition as models_composition  # noqa: PLC0415
    import app.modules.notifications.telegram as telegram_module  # noqa: PLC0415

    monkeypatch.setattr(auth_composition, "GoogleIdTokenVerifier", TokenGoogleVerifier)
    original_stack = models_composition.build_models_stack

    def scripted_stack(*args: Any, **kwargs: Any) -> Any:
        stack = original_stack(*args, **kwargs)
        stack.gateway = cast(Any, model)
        return stack

    monkeypatch.setattr(models_composition, "build_models_stack", scripted_stack)
    monkeypatch.setattr(jobs_composition, "build_fcm_client", lambda settings: fcm.client())
    monkeypatch.setattr(telegram_module, "HttpTelegramBotApi", FakeBotApi)

    transport = transport_factory(world)
    connector_init = McpConnector.__init__
    oauth_init = OAuthClient.__init__

    def connector(self: McpConnector, **kwargs: Any) -> None:
        kwargs["resolver"] = kwargs.get("resolver") or resolver
        kwargs["http_transport"] = kwargs.get("http_transport") or transport
        connector_init(self, **kwargs)

    def oauth(self: OAuthClient, **kwargs: Any) -> None:
        kwargs["resolver"] = kwargs.get("resolver") or resolver
        kwargs["http_transport"] = kwargs.get("http_transport") or transport
        oauth_init(self, **kwargs)

    monkeypatch.setattr(McpConnector, "__init__", connector)
    monkeypatch.setattr(OAuthClient, "__init__", oauth)
    # Fast worker loops; the production defaults are seconds.
    for name, value in {
        "JOBS_POLL_SECONDS": "0.05",
        "JOBS_RETRY_BASE_SECONDS": "0",
        "JOBS_DEADLINE_SCAN_SECONDS": "0.05",
        "JOBS_SHUTDOWN_GRACE_SECONDS": "1",
    }.items():
        monkeypatch.setenv(name, value)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[FastAPI]:
    """Run the app's lifespan in its own task, like uvicorn (anyio cancel scopes of ``/mcp``)."""
    ready, stop = asyncio.Event(), asyncio.Event()
    failure: list[BaseException] = []

    async def runner() -> None:
        try:
            async with application.router.lifespan_context(application):
                ready.set()
                await stop.wait()
        except BaseException as error:  # surfaced to the caller below
            failure.append(error)
            ready.set()

    task = asyncio.create_task(runner())
    await ready.wait()
    if failure:
        raise failure[0]
    try:
        yield application
    finally:
        stop.set()
        await task
        if failure:
            raise failure[0]


def compose(settings: Settings) -> FastAPI:
    """The server's own application factory (``install_runtime_api``)."""
    return create_app(settings)


@asynccontextmanager
async def mvp_world(monkeypatch: pytest.MonkeyPatch, *, linear: bool = True) -> AsyncIterator[Mvp]:
    if TEST_DATABASE_URL is None or TEST_REDIS_URL is None:
        pytest.skip("MIA_TEST_DATABASE_URL and MIA_TEST_REDIS_URL are required")
    if linear:
        from tests.plugins.linear.test_linear import recorded_linear  # noqa: PLC0415

        monkeypatch.setattr(remote_world, "build_mcp_server", recorded_linear)
    admin_id = await reset_database()
    model = ScriptedModel()
    fcm = FakeFcm()
    auth_server = AuthorizationServer(clock=_RealClock(), access_ttl=3600)  # type: ignore[arg-type]
    world = RemoteWorld(auth_server, require_auth=True, camel_case_tool=False)
    resolver = FakeResolver()
    patch_outside_world(monkeypatch, model, fcm, world, resolver)
    FakeBotApi.instances.clear()
    settings = _settings(admin_id)
    application = compose(settings)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(running(world))
        await stack.enter_async_context(lifespan(application))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url=PUBLIC_BASE
        )
        await stack.enter_async_context(client)
        factory = application.state.runtime.database.session_factory
        mvp = Mvp(
            app=application,
            client=client,
            model=model,
            fcm=fcm,
            remote=world,
            auth_server=auth_server,
            resolver=resolver,
            admin_id=admin_id,
            settings=settings,
            factory=factory,
            stack=stack,
        )
        try:
            yield mvp
        finally:
            await application.state.runs.aclose()


@asynccontextmanager
async def restarted(mvp: Mvp) -> AsyncIterator[FastAPI]:
    """A second process on the same database (worker restart): fresh app, fresh in-memory state."""
    application = compose(mvp.settings)
    async with lifespan(application):
        try:
            yield application
        finally:
            await application.state.runs.aclose()
