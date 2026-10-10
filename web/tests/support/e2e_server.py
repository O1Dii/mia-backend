"""Backend for the web Playwright tests (M30): the production wiring with fake outside worlds.

Real: PostgreSQL, Redis, auth (cookie sessions), workspace, store, installs, connections (OAuth
with PKCE/resource indicator against an in-process authorization server), tool router, approvals,
run engine, feed, model-connection API. Fake: Google ID tokens (``google:<subject>``), the model
(scripted by the last user message) and the remote MCP server plus its authorization server.

Run from the repository root with ``MIA_TEST_DATABASE_URL`` and ``MIA_TEST_REDIS_URL`` set::

    uv run python web/tests/support/e2e_server.py --port 8130 --public-origin http://127.0.0.1:5230

Test-only routes live under ``/__e2e`` and exist only in this process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import app.modules.auth.composition as auth_composition  # noqa: E402
import app.modules.models.composition as models_composition  # noqa: E402
import uvicorn  # noqa: E402
from app.api.v1.approvals import build_approvals_router  # noqa: E402
from app.api.v1.tiles import install_tiles  # noqa: E402
from app.composition import AuthServiceAuthenticator  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.database import Database  # noqa: E402
from app.core.ports.model_gateway import (  # noqa: E402
    ModelDone,
    ModelEvent,
    ModelInfo,
    ModelRequest,
    ModelToolCall,
    TextDelta,
    ToolCallRequested,
)
from app.core.ports.types import DEFAULT_MODEL  # noqa: E402
from app.core.runtime import Runtime  # noqa: E402
from app.main import create_app  # noqa: E402
from app.modules.auth.composition import install_auth, request_authenticator  # noqa: E402
from app.modules.connections.oauth.client import OAuthClient  # noqa: E402
from app.modules.connections.oauth.service import ConnectionService  # noqa: E402
from app.modules.conversations.composition import install_chats  # noqa: E402
from app.modules.jobs.composition import install_jobs  # noqa: E402
from app.modules.memory import install_memory  # noqa: E402
from app.modules.models.composition import install_models  # noqa: E402
from app.modules.models.gateway import ProbeResult  # noqa: E402
from app.modules.notifications.telegram import build_telegram  # noqa: E402
from app.modules.notifications.telegram_api import InlineButton, SendResult  # noqa: E402
from app.modules.plugins.builtin import BUILTIN_ROOT  # noqa: E402
from app.modules.plugins.catalog.package import load_package  # noqa: E402
from app.modules.plugins.catalog.showcase import showcase_for  # noqa: E402
from app.modules.plugins.composition import build_builtin_plugins  # noqa: E402
from app.modules.plugins.install.composition import (  # noqa: E402
    build_catalog_stack,
    install_store,
)
from app.modules.plugins.registry import PluginRegistry, SqlInstallationReader  # noqa: E402
from app.modules.runs.composition import install_runs  # noqa: E402
from app.modules.tools.approvals import SqlApprovalService  # noqa: E402
from app.modules.tools.audit import SqlToolAudit  # noqa: E402
from app.modules.tools.composition import ToolStack  # noqa: E402
from app.modules.tools.core_tools import CoreToolRegistry  # noqa: E402
from app.modules.tools.device import InMemoryDeviceToolChannel  # noqa: E402
from app.modules.tools.mcp_client import McpConnector  # noqa: E402
from app.modules.tools.router import McpToolRouter, RouterConfig  # noqa: E402
from app.modules.users import UserRepository  # noqa: E402
from app.modules.workspace.composition import install_workspace  # noqa: E402
from app.plugins.builtin.telegram.host import BotLinkOffer, bind_host  # noqa: E402
from app.worker.main import Worker  # noqa: E402
from fastapi import APIRouter, FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from sqlalchemy import text  # noqa: E402
from tests.catalog.remote_world import (  # noqa: E402
    AuthorizationServer,
    FakeResolver,
    RemoteWorld,
    browser_authorize,
    running,
    transport_factory,
)
from tests.models.auth_world import CLIENT_ID as CHATGPT_CLIENT_ID  # noqa: E402
from tests.models.auth_world import AuthWorld  # noqa: E402
from tests.workspace.harness import CLIENT_ID, TokenGoogleVerifier, migrate_to_head  # noqa: E402

ADMIN_SUB = "e2e-store-admin"
TRACKER_ID = "team.tracker"
UPDATER_ID = "team.updater"
TILES_DIR = Path(__file__).resolve().parent / "tiles"


def fake_apk() -> Path:
    folder = Path(tempfile.mkdtemp(prefix="mia-e2e-apk-"))
    apk = folder / "mia.apk"
    apk.write_bytes(b"PK\x03\x04" + bytes(2 * 1024 * 1024))
    info = {"version_name": "0.1.0-e2e", "version_code": 1}
    (folder / "mia.apk.json").write_text(json.dumps(info))
    return apk


class RealClock:
    def __call__(self) -> float:
        return time.time()

    @property
    def now(self) -> float:
        return time.time()


class Script:
    """What the fake model does; ``structured`` is toggled by ``POST /__e2e/model-mode``."""

    structured = True
    model_calls = 0
    """How many times the model was asked: dashboard tiles must never raise it."""
    link_leaks = 0
    """Model requests that contained a Telegram link (M41-R1: must stay 0)."""


BOT_LINK = "https://t.me/mia_e2e_bot?start=E2E-ONE-TIME-TOKEN"


class E2EBotApi:
    """The Telegram Bot API without a network (M77): remembers what the bot said, per chat."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

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


def telegram_chat_of(user_id: str) -> int:
    """A private chat id that is the same for the same user and differs between users."""
    return 1_000_000 + uuid.UUID(user_id).int % 1_000_000_000


class E2ETelegramHost:
    """``builtin.telegram`` without a phone or the Bot API: only ``connect_bot`` works.

    «просроченн…» in the user's message yields a link that has already expired.
    """

    expired_next = False

    async def candidates(self, identity: Any, to: str) -> list[Any]:
        return []

    async def hand_off(self, identity: Any, recipient: Any, text_value: str) -> Any:
        raise RuntimeError("no phone in the web tests")

    async def bot_link(self, identity: Any) -> BotLinkOffer:
        shift = -60 if E2ETelegramHost.expired_next else 15 * 60
        expires = datetime.now(UTC) + timedelta(seconds=shift)
        return BotLinkOffer(
            already_linked=False,
            deep_link=BOT_LINK,
            expires_at=expires.isoformat().replace("+00:00", "Z"),
        )


def answer(speech: str, display: dict[str, Any] | None = None, followups: list[str] | None = None):
    return json.dumps(
        {"speech": speech, "display": display, "followups": followups or []}, ensure_ascii=False
    )


class ScriptedGateway:
    """A model that answers by keyword; tool calls use the tools the router offers."""

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        return ModelInfo(
            ref="e2e/scripted",
            provider="e2e",
            supports_tools=True,
            supports_structured_output=Script.structured,
            verified=True,
        )

    connections: Any = None

    async def probe(self, user_id: Any, connection_id: Any) -> ProbeResult:
        await self.connections.record_test(connection_id, ok=True, error_code=None)
        return ProbeResult(ok=True, latency_ms=12, error_code=None)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        first = request.messages[0].content or ""
        if first.startswith("Придумай короткое название"):
            # Chat titling (M74): «Модельное название» + the first words of the first question.
            asked = (request.messages[-1].content or "").split("Пользователь:", 1)[-1]
            words = asked.split("\n", 1)[0].split()[:3]
            yield TextDelta("Модельное " + " ".join(words).lower().rstrip("!?.,"))
            yield ModelDone("stop")
            return
        Script.model_calls += 1
        if any("t.me" in (m.content or "") for m in request.messages):
            Script.link_leaks += 1
        last = request.messages[-1]
        names = [tool.name for tool in request.tools]
        user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
        lowered = user.lower()
        if last.role == "tool":
            body = json.loads(last.content) if last.content.startswith("{") else {}
            status = body.get("status")
            if status == "succeeded" and "бота" in lowered:
                reply = answer("Открой ссылку на экране: «Открыть Telegram», затем Start.")
            elif status == "succeeded" and "запомни" in lowered:
                reply = answer("Предложила запомнить: подтвердите на странице «Память».")
            elif status == "succeeded":
                reply = answer(
                    "Готово, задача создана." if "создай" in lowered else "Вот что нашлось.",
                    {"kind": "card", "title": "Tracker", "markdown": "**A-3** — результат готов"},
                    ["Покажи список задач"],
                )
            elif status == "denied":
                reply = answer("Хорошо, ничего не делаю.")
            else:
                reply = answer("Не получилось выполнить действие.")
            for event in self._text(reply):
                yield event
            return
        wanted = None
        if "запомни" in lowered:
            fact = user.split("что", 1)[-1].strip().capitalize() if "что" in lowered else user
            wanted = (
                "remember",
                {
                    "text": fact,
                    "category": "home",
                    "person": {"name": "Яна", "relation": "жена"},
                },
            )
        elif "создай задачу" in lowered:
            wanted = (
                "create_issue",
                {"title": user.split("задачу", 1)[1].strip() or "Без названия"},
            )
        elif "список задач" in lowered:
            wanted = ("list_issues", {})
        elif "бота" in lowered:
            E2ETelegramHost.expired_next = "просроченн" in lowered
            wanted = ("connect_bot", {})
        if wanted is not None:
            name = next((n for n in names if n == f"memory.{wanted[0]}"), None) or next(
                (n for n in names if n.endswith(f".{wanted[0]}") and n != "memory.remember"), None
            )
            if name is not None:
                call = ModelToolCall(call_id="call-1", name=name, arguments=wanted[1])
                yield ToolCallRequested(call)
                yield ModelDone("tool_calls")
                return
            for event in self._text(answer("Этот инструмент не установлен.")):
                yield event
            return
        if "в фоне" in lowered and "background.start" in names:
            # A long task asked for in the chat (M74: the result card returns to this chat).
            call = ModelToolCall(
                call_id="call-1", name="background.start", arguments={"goal": user}
            )
            yield ToolCallRequested(call)
            yield ModelDone("tool_calls")
            return
        if "таблиц" in lowered:
            table = (
                "Вот проекты:\n\n"
                "| Проект | Статус | Задач | Ответственный | Ближайший срок | Комментарий |\n"
                "|:--|:-:|--:|---|---|---|\n"
                "| Мия | **идёт** | 12 | Алексей | 15 октября | Релиз веб-версии |\n"
                "| [Сайт](https://example.test/site) | пауза | 3 | Яна | не задан | Ждём макеты |\n"
                "| Очень длинное название проекта для проверки прокрутки | готов | 40 | Олег "
                "| 1 сентября | Закрыт |\n"
            )
            reply = answer("Вот проекты таблицей.", {"kind": "text", "markdown": table})
        elif "покажи таймеры" in lowered:
            reply = answer(
                "Вот ваши таймеры.",
                {
                    "kind": "app",
                    "plugin_id": "builtin.timer",
                    "resource_uri": "ui://builtin.timer/tile",
                    "structured_content": {"items": [], "server_time_ms": 0},
                    "fallback_markdown": "Таймеры: плитка недоступна в этом окне.",
                },
            )
        elif "карточк" in lowered:
            reply = answer(
                "Вот карточка на экране.",
                {"kind": "card", "title": "Сегодня", "markdown": "- 10:00 Олег\n- 13:00 Команда"},
                ["Что дальше?"],
            )
        elif "длинн" in lowered:
            reply = answer("Это длинный ответ.", {"kind": "text", "markdown": "строка\n\n" * 40})
        else:
            reply = answer(
                "Привет! Я Мия, это тестовый ответ.",
                {"kind": "text", "markdown": "Привет! Я **Мия**, это тестовый ответ."},
                ["Расскажи ещё", "Создай задачу Купить молоко"],
            )
        for event in self._text(reply):
            yield event

    def _text(self, reply: str) -> list[ModelEvent]:
        if Script.structured:
            return [TextDelta(reply), ModelDone("stop")]
        data = json.loads(reply)
        text_out = data["display"]["markdown"] if data["display"] else data["speech"]
        return [TextDelta(text_out[:20]), TextDelta(text_out[20:]), ModelDone("stop")]


def tracker_manifest(
    url: str,
    plugin_id: str = TRACKER_ID,
    name: str = "Tracker",
    version: str = "1.0.0",
    notify: bool = False,
    tiles: bool = False,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schemaVersion": "mia.plugin/1",
        "id": plugin_id,
        "version": version,
        "name": name,
        "listing": {
            "description": "Задачи из тестового трекера: список и создание.",
            "category": "work",
        },
        "server": {"kind": "remote", "url": url, "auth": "oauth"},
        "skills": ["SKILL.md"],
        "tools": {
            "list_issues": {"risk": "read", "voice": True},
            "create_issue": {"risk": "write_external"},
        },
        "permissions": {
            "network": ["mcp.test.example"],
            "llm": {"viaHost": False},
            "memory": [],
            "notify": notify,
        },
    }
    if tiles:
        manifest["surfaces"] = {
            "tiles": [
                {
                    "id": "issues",
                    "title": "Задачи Tracker",
                    "resourceUri": f"ui://{plugin_id}/issues",
                    "bundled": "ui/issues.html",
                    "sizes": ["2x2", "4x2"],
                    "refresh": {"tool": "list_issues", "everySeconds": 60},
                },
                {
                    "id": "probe",
                    "title": "Проба изоляции",
                    "resourceUri": f"ui://{plugin_id}/probe",
                    "bundled": "ui/probe.html",
                    "sizes": ["2x2"],
                },
            ]
        }
    return manifest


async def prepare(public_origin: str) -> str:
    """Migrate, empty the disposable database, create the store admin; return its id."""
    await asyncio.to_thread(migrate_to_head)
    database = Database(os.environ["MIA_TEST_DATABASE_URL"])
    async with database.session_factory() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE users CASCADE"))
        await session.execute(text("TRUNCATE TABLE plugin_releases"))
        admin = await UserRepository(session).create(
            google_sub=ADMIN_SUB,
            email="admin@example.test",
            email_verified=True,
            locale="ru-BY",
            timezone="Europe/Minsk",
        )
    await database.close()
    return str(admin.id)


def build_app(admin_id: str, public_origin: str) -> FastAPI:
    settings = Settings(
        app_env="test",
        log_level="WARNING",
        database_url=os.environ["MIA_TEST_DATABASE_URL"],
        redis_url=os.environ["MIA_TEST_REDIS_URL"],
        google_client_id=CLIENT_ID,
        public_base_url=public_origin,
        store_admin_user_ids=admin_id,
        tools_list_cache_seconds=0,
        auth_nonce_rate_limit=1000,
        auth_exchange_rate_limit=1000,
        auth_authenticated_rate_limit=5000,
        # Like the hosted server (M62-T1): ChatGPT sign-in completes by the pasted address, and
        # a small stand-in APK is offered for download.
        chatgpt_plan_loopback_listener=False,
        android_apk_path=str(fake_apk()),
    )
    auth_composition.GoogleIdTokenVerifier = TokenGoogleVerifier  # type: ignore[misc,assignment]
    app = create_app(settings, install_modules=False)
    runtime: Runtime = app.state.runtime
    auth_service = install_auth(app, settings, database=runtime.database, redis=runtime.redis)
    bearer = AuthServiceAuthenticator(auth_service)
    gateway = ScriptedGateway()

    original = models_composition.build_models_stack

    # The fake auth.openai.com for «Войти в ChatGPT» (token endpoint, JWKS; M62-T1).
    chatgpt = AuthWorld()

    def scripted_stack(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("transport", chatgpt.transport)
        stack = original(*args, **kwargs)
        stack.gateway = gateway
        return stack

    models_composition.build_models_stack = scripted_stack  # type: ignore[assignment]
    models = install_models(app, settings, runtime, bearer)
    gateway.connections = models.connections

    clock = RealClock()
    auth_server = AuthorizationServer(clock=clock, access_ttl=3600)  # type: ignore[arg-type]
    world = RemoteWorld(auth_server, require_auth=True, camel_case_tool=False)
    resolver = FakeResolver()
    oauth = OAuthClient(resolver=resolver, http_transport=transport_factory(world))
    builtin = build_builtin_plugins(runtime, model_gateway=gateway)  # type: ignore[arg-type]
    bind_host(E2ETelegramHost())  # type: ignore[arg-type]
    stack = build_catalog_stack(settings, runtime, builtin)
    from tests.catalog.harness import _Bearer  # noqa: PLC0415

    stack.service._connector_factory = lambda token: McpConnector(  # noqa: SLF001
        server_factory=builtin.server_factory,
        credentials=_Bearer(token),
        resolver=resolver,
        http_transport=transport_factory(world),
    )
    workspace = install_workspace(app, database=runtime.database, catalog=stack.catalog)
    connections = ConnectionService(
        runtime.database.session_factory,
        models.vault,
        oauth,
        workspace,
        public_base_url=public_origin,
    )
    workspace.uninstall_hooks.append(connections.revoke_installation)
    connector = McpConnector(
        server_factory=builtin.server_factory,
        credentials=connections,
        resolver=resolver,
        http_transport=transport_factory(world),
    )
    registry = PluginRegistry(
        SqlInstallationReader(runtime.database.session_factory), stack.manifest_source
    )
    channel = InMemoryDeviceToolChannel()
    core_tools = CoreToolRegistry()
    approvals = SqlApprovalService(runtime.database.session_factory)
    router = McpToolRouter(
        registry=registry,
        connector=connector,
        audit=SqlToolAudit(runtime.database.session_factory),
        approvals=approvals,
        device_channel=channel,
        core_tools=core_tools,
        config=RouterConfig(call_timeout_s=10, list_cache_s=0, approval_ttl_s=900),
    )
    tools = ToolStack(router, approvals, channel, core_tools, registry, connector)
    install_store(
        app,
        settings,
        runtime,
        catalog=stack,
        connections=connections,
        tools=tools,
        workspace=workspace,
    )
    app.include_router(
        build_approvals_router(service=approvals, authenticator=request_authenticator(app))
    )
    install_tiles(
        app,
        registry=registry,
        connector=connector,
        router=router,
        authenticator=request_authenticator(app),
    )
    memory = install_memory(app, runtime, vault=models.vault, tools=tools)
    runs = install_runs(
        app,
        settings,
        runtime,
        authenticator=bearer,
        model_gateway=gateway,  # type: ignore[arg-type]
        tool_router=router,
        approvals=approvals,
        memory=memory.service,
    )
    memory.tools.bind_publisher(runs.publish)
    jobs = install_jobs(app, settings, runtime, runs=runs, core_tools=core_tools)
    # The Telegram bot with a fake Bot API (M77): linking, the «Присылать сюда…» question, delivery.
    bot_api = E2EBotApi()
    telegram = build_telegram(
        settings, runtime, api=bot_api, preferences=jobs.notifications.preferences  # type: ignore[arg-type]
    )
    assert telegram.channel is not None and telegram.bot is not None
    jobs.notifications.channels.register(telegram.channel)
    web_auth = request_authenticator(app)
    # Chats (M74), the feed per chat and «Сейчас»; titles come from the scripted model.
    install_chats(app, runtime, runs=runs, gateway=gateway)  # type: ignore[arg-type]

    async def publish_remote(
        plugin_id: str, name: str, version: str, notify: bool, tiles: bool = False
    ) -> None:
        package = Path(tempfile.mkdtemp(prefix="mia-e2e-")) / plugin_id
        package.mkdir()
        manifest = tracker_manifest(
            "https://mcp.test.example/mcp", plugin_id, name, version, notify, tiles
        )
        if tiles:
            (package / "ui").mkdir()
            for tile in ("issues", "probe"):
                (package / "ui" / f"{tile}.html").write_text(
                    (TILES_DIR / f"{tile}.html").read_text("utf-8"), "utf-8"
                )
        (package / "mia.json").write_text(json.dumps(manifest), "utf-8")
        (package / "SKILL.md").write_text("---\ndescription: Issues\n---\nUse it.\n", "utf-8")
        await stack.service.publish(
            load_package(package), actor_user_id=admin_id, discover_token=auth_server.mint_token()
        )

    test_routes = APIRouter(prefix="/__e2e")

    @test_routes.post("/publish-updater-v2")
    async def publish_updater_v2() -> JSONResponse:
        """A new release that also asks to notify: installed copies must re-consent."""
        await publish_remote(UPDATER_ID, "Updater", "1.1.0", True)
        return JSONResponse({"version": "1.1.0"})

    @test_routes.get("/authorize")
    async def authorize(request: Request) -> JSONResponse:
        """What the browser does at the provider: consent at once and return the redirect."""
        url = f"https://auth.test.example/authorize?{request.url.query}"
        return JSONResponse({"location": await browser_authorize(world, url)})

    @test_routes.get("/chatgpt-authorize")
    async def chatgpt_authorize(request: Request) -> JSONResponse:
        """ChatGPT consent at once: where the browser lands (the unreachable loopback address)."""
        query = request.query_params
        chatgpt.nonce = query["nonce"]
        landed = (
            query["redirect_uri"]
            + "?"
            + urlencode(
                {"code": "e2e-code", "state": query["state"], "client_id": CHATGPT_CLIENT_ID}
            )
        )
        return JSONResponse({"location": landed})

    @test_routes.get("/world")
    async def world_state() -> JSONResponse:
        return JSONResponse(
            {
                "calls": world.calls,
                "big_calls": world.big_calls,
                "revocations": auth_server.revocations,
                "registrations": len(auth_server.registrations),
                "authorize_requests": auth_server.authorize_requests,
                "model_calls": Script.model_calls,
                "link_leaks": Script.link_leaks,
            }
        )

    @test_routes.post("/telegram/link")
    async def telegram_link(request: Request) -> JSONResponse:
        """The user opened the deep link and pressed Start in the bot."""
        principal = await web_auth.authenticate(request)
        offer = await telegram.links.create_offer(uuid.UUID(principal.user_id))
        chat = telegram_chat_of(principal.user_id)
        assert telegram.bot is not None
        await telegram.bot.handle_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": chat, "type": "private"},
                    "from": {"id": chat, "is_bot": False},
                    "text": f"/start {offer.token}",
                },
            }
        )
        return JSONResponse({"chat_id": chat})

    @test_routes.post("/telegram/press")
    async def telegram_press(request: Request) -> JSONResponse:
        """The user pressed an inline button under the bot's question."""
        principal = await web_auth.authenticate(request)
        chat = telegram_chat_of(principal.user_id)
        assert telegram.bot is not None
        await telegram.bot.handle_update(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "e2e-callback",
                    "from": {"id": chat, "is_bot": False},
                    "message": {"message_id": 7, "chat": {"id": chat, "type": "private"}},
                    "data": (await request.json())["data"],
                },
            }
        )
        return JSONResponse({"ok": True})

    @test_routes.get("/telegram/sent")
    async def telegram_sent(request: Request) -> JSONResponse:
        principal = await web_auth.authenticate(request)
        chat = telegram_chat_of(principal.user_id)
        return JSONResponse({"sent": [m for m in bot_api.sent if m["chat_id"] == chat]})

    @test_routes.post("/model-mode")
    async def model_mode(request: Request) -> JSONResponse:
        Script.structured = bool((await request.json()).get("structured", True))
        return JSONResponse({"structured": Script.structured})

    app.include_router(test_routes)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(application), running(world):
            worker = Worker(jobs)
            stop = asyncio.Event()
            worker_task = asyncio.create_task(worker.run(stop))
            admin_publish = stack.service.publish
            for name in sorted(p.parent.name for p in BUILTIN_ROOT.glob("*/mia.json")):
                built = load_package(BUILTIN_ROOT / name)
                await admin_publish(
                    built, actor_user_id=admin_id, store_meta=showcase_for(built.plugin_id)
                )
            await publish_remote(TRACKER_ID, "Tracker", "1.0.0", False, tiles=True)
            await publish_remote(UPDATER_ID, "Updater", "1.0.0", False)
            try:
                yield
            finally:
                stop.set()
                await worker_task

    app.router.lifespan_context = lifespan  # type: ignore[assignment]
    _ = runs
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8130)
    parser.add_argument("--public-origin", default="http://127.0.0.1:5230")
    args = parser.parse_args()
    admin_id = asyncio.run(prepare(args.public_origin))
    app = build_app(admin_id, args.public_origin)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
