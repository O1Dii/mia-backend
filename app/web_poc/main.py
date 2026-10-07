"""Local-only web POC API. Run with --host 127.0.0.1; no .env or database needed."""

import asyncio
import hashlib
import io
import json
import secrets
import time
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.modules.auth.google import (
    GoogleIdentity,
    GoogleIdTokenVerifier,
    GoogleVerificationUnavailableError,
    GoogleVerifier,
    InvalidGoogleTokenError,
)
from app.web_poc.accounts import AccountStore, WebSettings
from app.web_poc.agent import run_agent
from app.web_poc.extensions import ExtensionAuth, Grant
from app.web_poc.internet import Internet
from app.web_poc.providers import (
    PROVIDERS,
    Connection,
    ProviderFailure,
    ProviderId,
    generate,
    resolve_url,
)
from app.web_poc.runtime import (
    BUILTINS,
    AppManifest,
    DigestSettings,
    PluginRuntime,
    Workspace,
    declarations,
)

COOKIE = "mia_web_session"
SESSION_TTL = 8 * 3600
MAX_BODY = 150_000


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: ProviderId
    label: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=150, pattern=r"^[a-zA-Z0-9_./:@-]+$")
    api_key: SecretStr = Field(default=SecretStr(""), max_length=4096, repr=False)
    base_url: str | None = Field(default=None, max_length=200)
    tool_mode: Literal["native", "json"] = "native"

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Label cannot be blank")
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def clean_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if any(ord(character) < 33 or ord(character) > 126 for character in key):
            raise ValueError("API key must contain printable ASCII without spaces")
        return SecretStr(key)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=24000)


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str | None = Field(default=None, max_length=50)
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=8, max_length=80)
    use_tools: bool = True
    plugin_id: str | None = Field(default=None, max_length=80)
    messages: list[Message] = Field(min_length=1, max_length=40)


class Selection(BaseModel):
    connection_id: str = Field(max_length=50)


class InstallInput(BaseModel):
    plugin_id: str = Field(max_length=80)
    manifest: AppManifest | None = None


class ActionInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=8, max_length=80)


class Widget(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    plugin_id: str = Field(max_length=80)
    size: Literal["small", "medium", "wide"] = "medium"


class LayoutInput(BaseModel):
    widgets: list[Widget] = Field(max_length=30)


class PairStart(BaseModel):
    plugin_id: str = Field(pattern=r"^(mia|community)\.[a-z0-9-]{2,48}$")


class PairPoll(BaseModel):
    device_code: str = Field(min_length=20, max_length=100)


class PairCode(BaseModel):
    user_code: str = Field(min_length=8, max_length=8)


class GoogleSignIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    credential: SecretStr = Field(min_length=20, max_length=12000, repr=False)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rating: int = Field(ge=1, le=5)
    text: str = Field(min_length=5, max_length=2000)


AUTH_COOKIE = "mia_account"
NONCE_COOKIE = "mia_google_nonce"


def create_web_app(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    internet: Internet | None = None,
    settings: WebSettings | None = None,
    google_verifier: GoogleVerifier | None = None,
) -> FastAPI:
    config = settings or WebSettings.environment()
    config.validate()
    accounts = AccountStore(config.database_path)
    verifier = google_verifier or GoogleIdTokenVerifier()
    account_workspaces: set[str] = set()
    saved_revisions: dict[str, int] = {}

    def persist() -> None:
        for user_id in account_workspaces:
            state = sessions.get(user_id)
            if state is not None and saved_revisions.get(user_id) != state.revision:
                accounts.save(state)
                saved_revisions[user_id] = state.revision

    sessions: dict[str, Workspace] = {}
    runtime = PluginRuntime(internet, transport)
    extensions = ExtensionAuth()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def expire_sessions() -> None:
            while True:
                await asyncio.sleep(15)
                now = time.monotonic()
                for token in list(sessions):
                    if sessions[token].expires <= now:
                        del sessions[token]
                extensions.prune()
                persist()
                for state in list(sessions.values()):
                    digest = state.states.get("mia.digest", {})
                    interval = digest.get("interval_minutes", 0)
                    if (
                        "mia.digest" in state.installed
                        and interval
                        and not state.busy.locked()
                        and time.time() * 1000 >= digest.get("next_refresh", 0)
                    ):
                        digest["next_refresh"] = int(time.time() * 1000) + interval * 60000
                        connection = state.connections.get(state.active_connection_id)
                        if connection:
                            async with state.busy:
                                outcome = await runtime.execute(
                                    state,
                                    "mia_digest__refresh",
                                    {"topics": digest.get("topics", "технологии")},
                                    str(uuid4()),
                                    connection=connection,
                                )
                                if not outcome["ok"]:
                                    digest["error"] = outcome["error"]

        cleaner = asyncio.create_task(expire_sessions())
        try:
            yield
        finally:
            cleaner.cancel()
            await asyncio.gather(cleaner, return_exceptions=True)
            persist()
            sessions.clear()
            accounts.close()

    app = FastAPI(title="MIA Web", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(127\.0\.0\.1|localhost)(:\d+)?",
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "localhost",
            "127.0.0.1",
            "[::1]",
            "testserver",
            *([urlsplit(config.public_origin).hostname] if config.hosted else []),
        ],
    )

    @app.middleware("http")
    async def local_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if (
            not config.hosted
            and request.client
            and request.client.host not in {"127.0.0.1", "::1", "testclient"}
        ):
            return JSONResponse({"detail": "Этот POC работает только на localhost."}, 403)
        # OAuth redirects retain cross-site metadata. Permit loading only the
        # public SPA entry document; its subsequent API calls remain same-origin.
        page_navigation = (
            config.hosted
            and bool(config.static_dir)
            and request.method in {"GET", "HEAD"}
            and request.url.path == "/"
            and request.headers.get("sec-fetch-mode") == "navigate"
            and request.headers.get("sec-fetch-dest") == "document"
        )
        origin = request.headers.get("origin")
        if origin and not page_navigation:
            parsed = urlsplit(origin)
            local_sdk = request.url.path.startswith("/api/sdk") and (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
            )
            valid_origin = (
                origin == config.public_origin
                if config.hosted
                else (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
            if not valid_origin and not local_sdk:
                return JSONResponse({"detail": "Запрос с внешнего сайта отклонён."}, 403)
        if (
            request.headers.get("sec-fetch-site") == "cross-site"
            and not request.url.path.startswith("/api/sdk")
            and not page_navigation
        ):
            return JSONResponse({"detail": "Запрос с внешнего сайта отклонён."}, 403)
        if request.method in {"POST", "DELETE"}:
            if request.url.path.startswith("/api/web") and request.headers.get("x-mia-web") != "1":
                return JSONResponse({"detail": "Откройте приложение MIA."}, 403)
            if (
                request.method == "POST"
                and request.headers.get("content-type", "").split(";")[0] != "application/json"
            ):
                return JSONResponse({"detail": "Ожидается JSON."}, 415)
            # Bound chunked bodies too; validation errors must never echo the body.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    return JSONResponse({"detail": "Запрос слишком большой."}, 413)
            request._body = bytes(body)
        response = await call_next(request)
        persist()
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Проверьте поля запроса и формат значений."}, 422)

    async def workspace(request: Request, response: Response) -> Workspace:
        now = time.monotonic()
        user = accounts.user(request.cookies.get(AUTH_COOKIE, ""))
        if user:
            user_id = user["id"]
            existing_account = sessions.get(user_id)
            if existing_account is None or existing_account.expires <= now:
                if len(sessions) >= 128:
                    raise HTTPException(
                        503, "Слишком много активных пространств. Попробуйте позже."
                    )
                existing_account = accounts.restore(user_id)
                sessions[user_id] = existing_account
                saved_revisions[user_id] = existing_account.revision
            account_workspaces.add(user_id)
            return existing_account
        if config.hosted:
            raise HTTPException(401, "Войдите через Google.")
        token = request.cookies.get(COOKIE, "")
        existing = sessions.get(token)
        if existing and existing.expires > now:
            return existing
        for stale in list(sessions):
            if sessions[stale].expires <= now:
                del sessions[stale]
        if len(sessions) >= 128:
            raise HTTPException(503, "Слишком много локальных сессий. Перезапустите POC.")
        token = secrets.token_urlsafe(32)
        session = Workspace(expires=now + SESSION_TTL)
        sessions[token] = session
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            max_age=SESSION_TTL,
            secure=request.url.scheme == "https",
            path="/api/web",
        )
        return session

    @app.get("/api/web/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "local-poc"}

    @app.get("/api/web/providers")
    async def providers() -> list[dict[str, str | bool]]:
        return [{"id": provider, **details} for provider, details in PROVIDERS.items()]

    def set_account_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            AUTH_COOKIE,
            token,
            max_age=30 * 86400,
            httponly=True,
            secure=config.hosted,
            samesite="strict",
            path=config.cookie_path,
        )

    @app.get("/api/web/auth/session")
    async def auth_session(request: Request) -> dict[str, Any]:
        return {
            "user": accounts.user(request.cookies.get(AUTH_COOKIE, "")),
            "required": config.hosted,
            "google_client_id": config.google_client_id,
            "google_hub": config.google_hub,
            "login_url": "/api/auth/google/login?return_to="
            + quote(config.cookie_path + "?signin=google", safe=""),
        }

    @app.post("/api/web/auth/nonce")
    async def auth_nonce(response: Response) -> dict[str, str]:
        if not config.google_client_id:
            raise HTTPException(503, "Google-вход не настроен.")
        try:
            cookie, nonce = accounts.challenge()
        except ValueError:
            raise HTTPException(429, "Слишком много попыток входа.") from None
        response.set_cookie(
            NONCE_COOKIE,
            cookie,
            max_age=300,
            httponly=True,
            secure=config.hosted,
            samesite="strict",
            path=config.cookie_path,
        )
        return {"nonce": nonce}

    @app.post("/api/web/auth/google")
    async def auth_google(
        payload: GoogleSignIn, request: Request, response: Response
    ) -> dict[str, Any]:
        nonce = accounts.consume_challenge(request.cookies.get(NONCE_COOKIE, ""))
        if not nonce or not config.google_client_id:
            raise HTTPException(401, "Попытка входа истекла. Попробуйте снова.")
        try:
            identity = await verifier.verify(
                payload.credential.get_secret_value(), audience=config.google_client_id, nonce=nonce
            )
            token, user = accounts.login(identity)
        except GoogleVerificationUnavailableError:
            raise HTTPException(503, "Google временно недоступен.") from None
        except (InvalidGoogleTokenError, ValueError):
            raise HTTPException(401, "Не удалось подтвердить Google-аккаунт.") from None
        set_account_cookie(response, token)
        response.delete_cookie(NONCE_COOKIE, path=config.cookie_path)
        return {"user": user}

    @app.post("/api/web/auth/hub")
    async def auth_hub(request: Request, response: Response) -> dict[str, Any]:
        # Reuse the already configured Google OAuth service on this trusted server.
        # No shared signing secrets or Google client secret are copied into MIA.
        cookie = request.cookies.get("mw_session", "")
        if not config.google_hub or not config.hosted or not cookie or len(cookie) > 4096:
            raise HTTPException(401, "Войдите через Google на главном экране.")
        try:
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
                cookies={"mw_session": cookie},
            ) as client:
                result = await client.get(config.public_origin + "/api/auth/me")
                result.raise_for_status()
                body = result.json()
            u = body["user"]
            if (
                not body.get("google_enabled")
                or not body.get("authenticated")
                or u.get("is_local", True)
            ):
                raise ValueError
            from uuid import UUID

            identity = GoogleIdentity(
                "hub:" + str(UUID(u["id"])), u["email"], True, u["name"], None, "ru"
            )
            token, user = accounts.login(identity)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise HTTPException(
                401, "Сессия Google не подтверждена. Выполните вход ещё раз."
            ) from None
        set_account_cookie(response, token)
        return {"user": user}

    @app.post("/api/web/auth/logout")
    async def auth_logout(request: Request, response: Response) -> dict[str, bool]:
        accounts.logout(request.cookies.get(AUTH_COOKIE, ""))
        response.delete_cookie(AUTH_COOKIE, path=config.cookie_path)
        return {"ok": True}

    @app.get("/api/web/plugins/{plugin_id}/reviews")
    async def reviews(plugin_id: str, request: Request) -> dict[str, Any]:
        if plugin_id not in BUILTINS:
            raise HTTPException(404, "Отзывы доступны для опубликованных плагинов.")
        user = accounts.user(request.cookies.get(AUTH_COOKIE, ""))
        return accounts.reviews(plugin_id, user["id"] if user else None)

    @app.post("/api/web/plugins/{plugin_id}/reviews")
    async def write_review(
        plugin_id: str,
        payload: ReviewInput,
        request: Request,
        session: Annotated[Workspace, Depends(workspace)],
    ) -> dict[str, Any]:
        user = accounts.user(request.cookies.get(AUTH_COOKIE, ""))
        if user is None:
            raise HTTPException(401, "Для отзыва войдите через Google.")
        if plugin_id not in BUILTINS or plugin_id not in session.installed:
            raise HTTPException(403, "Сначала установите этот плагин.")
        if len(payload.text.strip()) < 5:
            raise HTTPException(422, "Напишите хотя бы несколько слов.")
        accounts.review(plugin_id, user["id"], payload.rating, payload.text.strip())
        return accounts.reviews(plugin_id, user["id"])

    @app.delete("/api/web/plugins/{plugin_id}/reviews")
    async def delete_review(plugin_id: str, request: Request) -> dict[str, Any]:
        user = accounts.user(request.cookies.get(AUTH_COOKIE, ""))
        if user is None:
            raise HTTPException(401, "Войдите через Google.")
        accounts.remove_review(plugin_id, user["id"])
        return accounts.reviews(plugin_id, user["id"])

    @app.get("/api/web/connections")
    async def list_connections(
        session: Annotated[Workspace, Depends(workspace)],
    ) -> list[dict[str, str | bool]]:
        return [connection.public() for connection in session.connections.values()]

    @app.post("/api/web/connections", status_code=201)
    async def add_connection(
        payload: ConnectionInput,
        session: Annotated[Workspace, Depends(workspace)],
    ) -> dict[str, str | bool]:
        if len(session.connections) >= 20:
            raise HTTPException(409, "В POC можно сохранить до 20 подключений.")
        if config.hosted and PROVIDERS[payload.provider]["local"]:
            raise HTTPException(
                422,
                "Для локальной модели используйте Мию на своём компьютере. "
                "Удалённый connector ещё не подключён.",
            )
        if not PROVIDERS[payload.provider]["local"] and not payload.api_key.get_secret_value():
            raise HTTPException(422, "Для облачного провайдера нужен API-ключ.")
        try:
            url = resolve_url(payload.provider, payload.base_url)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        connection = Connection(
            str(uuid4()),
            payload.provider,
            payload.label.strip(),
            payload.model,
            url,
            payload.api_key,
            payload.tool_mode,
        )
        session.connections[connection.id] = connection
        session.revision += 1
        if not session.active_connection_id:
            session.active_connection_id = connection.id
        return connection.public()

    @app.delete("/api/web/connections/{connection_id}", status_code=204)
    async def remove_connection(
        connection_id: str, session: Annotated[Workspace, Depends(workspace)]
    ) -> None:
        if session.connections.pop(connection_id, None) is None:
            raise HTTPException(404, "Подключение не найдено.")
        if session.active_connection_id == connection_id:
            session.active_connection_id = ""
        session.revision += 1

    async def perform_chat(
        payload: ChatInput, session: Workspace, only_plugin: str | None = None
    ) -> dict[str, Any]:
        connection_id = (
            session.active_connection_id
            if only_plugin
            else (payload.connection_id or session.active_connection_id)
        )
        connection = session.connections.get(connection_id)
        if connection is None:
            raise HTTPException(
                404, "Подключение истекло или модель не выбрана. Добавьте модель заново."
            )
        if payload.messages[-1].role != "user":
            raise HTTPException(422, "Последнее сообщение должно быть от пользователя.")
        if sum(len(m.content) for m in payload.messages) > 48000:
            raise HTTPException(413, "История слишком большая. Начните новый диалог.")
        signature = hashlib.sha256(
            json.dumps([connection_id, only_plugin, payload.model_dump()], sort_keys=True).encode()
        ).hexdigest()
        cache_id = f"{only_plugin or 'web'}:{payload.request_id}"
        if cache_id in session.turns:
            previous, result = session.turns[cache_id]
            if previous != signature:
                raise HTTPException(409, "ID запроса уже использован для другого сообщения.")
            return result
        now = time.monotonic()
        session.calls = [stamp for stamp in session.calls if stamp > now - 60]
        if session.busy.locked() or len(session.calls) >= 10:
            raise HTTPException(429, "Дождитесь ответа или повторите запрос через минуту.")
        session.calls.append(now)
        async with session.busy:
            try:
                messages = [m.model_dump() for m in payload.messages]
                if payload.use_tools:
                    scope = only_plugin or payload.plugin_id
                    if scope is not None and scope not in session.installed:
                        raise HTTPException(403, "Сначала установите плагин.")
                    tool_list, skills = declarations(session, scope)

                    async def execute(
                        name: str, arguments: dict[str, Any], call_id: str
                    ) -> dict[str, Any]:
                        return await runtime.execute(
                            session,
                            name,
                            arguments,
                            call_id,
                            only_plugin=scope,
                            connection=connection,
                        )

                    async with asyncio.timeout(150):
                        answer = await run_agent(
                            connection,
                            messages,
                            tool_list,
                            skills,
                            execute,
                            transport=transport,
                            run_id=cache_id,
                        )
                    result = {
                        "text": answer.text,
                        "actions": answer.actions,
                        "warning": answer.warning,
                        "connection_id": connection.id,
                        "model": connection.model,
                    }
                else:
                    result = {
                        "text": await generate(connection, messages, transport=transport),
                        "actions": [],
                        "connection_id": connection.id,
                        "model": connection.model,
                    }
            except TimeoutError:
                # Never turn a partially executed turn into a blind retry.
                result = {
                    "text": (
                        "Лимит времени достигнут. Проверьте виджеты и журнал действий перед "
                        "повтором."
                    ),
                    "actions": [],
                    "warning": "Выполнение могло завершиться частично.",
                    "model": connection.model,
                }
            except ProviderFailure as error:
                raise HTTPException(error.status, str(error)) from None
            session.turns[cache_id] = (signature, result)
            if len(session.turns) > 100:
                del session.turns[next(iter(session.turns))]
            return result

    @app.post("/api/web/chat")
    async def chat(
        payload: ChatInput, session: Annotated[Workspace, Depends(workspace)]
    ) -> dict[str, Any]:
        return await perform_chat(payload, session)

    @app.get("/api/web/workspace")
    async def get_workspace(session: Annotated[Workspace, Depends(workspace)]) -> dict[str, Any]:
        return session.snapshot()

    @app.post("/api/web/connections/select")
    async def select_connection(
        payload: Selection, session: Annotated[Workspace, Depends(workspace)]
    ) -> dict[str, Any]:
        if payload.connection_id not in session.connections:
            raise HTTPException(404, "Подключение не найдено.")
        session.active_connection_id = payload.connection_id
        session.revision += 1
        return session.snapshot()

    @app.post("/api/web/plugins/install")
    async def install_plugin(
        payload: InstallInput, session: Annotated[Workspace, Depends(workspace)]
    ) -> dict[str, Any]:
        if len(session.installed) >= 60 and payload.plugin_id not in session.installed:
            raise HTTPException(409, "Лимит: 60 плагинов.")
        try:
            async with session.mutation:
                previous = session.installed.get(payload.plugin_id)
                session.install(payload.plugin_id, payload.manifest)
                if previous is not None and previous != session.installed[payload.plugin_id]:
                    extensions.revoke_installation(session, payload.plugin_id)
        except ValueError:
            raise HTTPException(422, "Некорректный manifest или неизвестный плагин.") from None
        return session.snapshot()

    @app.delete("/api/web/plugins/{plugin_id}")
    async def remove_plugin(
        plugin_id: str, session: Annotated[Workspace, Depends(workspace)]
    ) -> dict[str, Any]:
        try:
            async with session.mutation:
                session.remove(plugin_id)
                extensions.revoke_installation(session, plugin_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return session.snapshot()

    async def perform_action(
        payload: ActionInput,
        session: Workspace,
        only_plugin: str | None = None,
        request_prefix: str = "web-action",
    ) -> dict[str, Any]:
        now = time.monotonic()
        session.calls = [stamp for stamp in session.calls if stamp > now - 60]
        if session.busy.locked() or len(session.calls) >= 30:
            raise HTTPException(429, "Дождитесь текущего действия или повторите через минуту.")
        session.calls.append(now)
        async with session.busy:
            try:
                async with asyncio.timeout(150):
                    return await runtime.execute(
                        session,
                        payload.name,
                        payload.arguments,
                        f"{request_prefix}:{payload.request_id}",
                        only_plugin=only_plugin,
                        connection=session.connections.get(session.active_connection_id),
                    )
            except TimeoutError:
                return {
                    "ok": False,
                    "error": "Время ожидания истекло. Проверьте состояние виджета.",
                }

    @app.post("/api/web/actions")
    async def invoke(
        payload: ActionInput, session: Annotated[Workspace, Depends(workspace)]
    ) -> dict[str, Any]:
        result = await perform_action(payload, session)
        return {**result, "workspace": session.snapshot()}

    @app.post("/api/web/dashboard")
    async def dashboard(
        payload: LayoutInput, session: Annotated[Workspace, Depends(workspace)]
    ) -> dict[str, Any]:
        if any(w.plugin_id not in session.installed for w in payload.widgets) or len(
            {w.id for w in payload.widgets}
        ) != len(payload.widgets):
            raise HTTPException(
                422, "Виджеты должны принадлежать установленным плагинам и иметь уникальные ID."
            )
        session.layout = [w.model_dump() for w in payload.widgets]
        session.revision += 1
        return session.snapshot()

    @app.post("/api/web/digest/settings")
    async def digest_settings(
        payload: DigestSettings, session: Annotated[Workspace, Depends(workspace)]
    ) -> dict[str, Any]:
        if "mia.digest" not in session.installed:
            raise HTTPException(403, "Установите новостной плагин.")
        async with session.mutation:
            session.states.setdefault("mia.digest", {}).update(
                **payload.model_dump(),
                next_refresh=int(time.time() * 1000) + payload.interval_minutes * 60000,
            )
            session.revision += 1
        return session.snapshot()

    @app.post("/api/sdk/pair")
    async def start_pair(payload: PairStart) -> dict[str, str | int]:
        try:
            return extensions.start(payload.plugin_id)
        except ValueError as error:
            raise HTTPException(429, str(error)) from None

    @app.post("/api/sdk/pair/poll")
    async def poll_pair(payload: PairPoll) -> dict[str, str]:
        try:
            return extensions.poll(payload.device_code)
        except ValueError as error:
            raise HTTPException(410, str(error)) from None

    @app.post("/api/web/apps/inspect")
    async def inspect_pair(
        payload: PairCode, session: Annotated[Workspace, Depends(workspace)]
    ) -> dict[str, Any]:
        try:
            pairing = extensions.inspect(payload.user_code)
            manifest = session.installed.get(pairing.plugin_id)
            if not manifest:
                raise ValueError("Сначала установите плагин этого приложения.")
            return {
                "plugin_id": pairing.plugin_id,
                "name": manifest["name"],
                "permissions": manifest.get("permissions", []),
                "subject_id": session.subject_id,
            }
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/api/web/apps/approve")
    async def approve_pair(
        payload: PairCode, session: Annotated[Workspace, Depends(workspace)]
    ) -> dict[str, str]:
        try:
            grant = extensions.approve(session, payload.user_code)
            return {"id": grant.id, "plugin_id": grant.plugin_id}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.get("/api/web/apps")
    async def list_apps(session: Annotated[Workspace, Depends(workspace)]) -> list[dict[str, Any]]:
        extensions.prune()
        return [
            {"id": g.id, "plugin_id": g.plugin_id, "expires_in": int(g.expires - time.monotonic())}
            for g in extensions.grants.values()
            if g.workspace is session
        ]

    @app.delete("/api/web/apps/{grant_id}", status_code=204)
    async def revoke_app(grant_id: str, session: Annotated[Workspace, Depends(workspace)]) -> None:
        grant = next(
            (g for g in extensions.grants.values() if g.id == grant_id and g.workspace is session),
            None,
        )
        if grant is None:
            raise HTTPException(404, "Приложение не найдено.")
        grant.revoked = True

    async def sdk_grant(request: Request) -> Grant:
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise HTTPException(401, "Привяжите приложение к Мии.")
        try:
            return extensions.authenticate(value[7:])
        except ValueError as error:
            raise HTTPException(401, str(error)) from None

    @app.get("/api/sdk/state")
    async def sdk_state(grant: Annotated[Grant, Depends(sdk_grant)]) -> dict[str, Any]:
        state = grant.workspace
        model = state.connections.get(state.active_connection_id)
        return {
            "subject_id": state.subject_id,
            "plugin_id": grant.plugin_id,
            "revision": state.revision,
            "state": state.states.get(grant.plugin_id, {}),
            "model": {"label": model.label, "model": model.model} if model else None,
        }

    @app.post("/api/sdk/actions")
    async def sdk_action(
        payload: ActionInput, grant: Annotated[Grant, Depends(sdk_grant)]
    ) -> dict[str, Any]:
        return await perform_action(payload, grant.workspace, grant.plugin_id, grant.id)

    @app.post("/api/sdk/generate")
    async def sdk_generate(
        payload: ChatInput, grant: Annotated[Grant, Depends(sdk_grant)]
    ) -> dict[str, Any]:
        return await perform_chat(payload, grant.workspace, grant.plugin_id)

    @app.get("/api/web/downloads/timer.zip")
    async def download_timer() -> StreamingResponse:
        root = Path(__file__).resolve().parents[2] / "packages" / "mia-sdk"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("mia-sdk.mjs", "timer.html", "README.md"):
                content = (root / name).read_bytes()
                if name == "timer.html" and config.hosted:
                    content = content.replace(
                        b"new MiaClient({pluginId:",
                        (
                            "new MiaClient({baseUrl:"
                            + json.dumps(config.public_origin + config.base_path)
                            + ",pluginId:"
                        ).encode(),
                    )
                archive.writestr(name, content)
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="mia-timer-app.zip"'},
        )

    @app.get("/api/web/sdk/{asset}")
    async def sdk_asset(asset: str) -> FileResponse:
        if asset not in {"mia-sdk.mjs", "timer.html"}:
            raise HTTPException(404)
        root = Path(__file__).resolve().parents[2] / "packages" / "mia-sdk"
        return FileResponse(
            root / asset, media_type="text/javascript" if asset.endswith("mjs") else "text/html"
        )

    if config.static_dir:
        app.mount("/", StaticFiles(directory=config.static_dir, html=True), name="web")
    return app


app = create_web_app()
