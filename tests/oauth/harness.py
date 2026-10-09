"""M50 test world: Postgres, a CIMD client served over a mock network, the app API in process.

- ``FirstParty`` stands in for the M10 principal (header ``x-test-user``);
- client metadata documents are served behind the real SSRF guard (a fake resolver answers a
  public IP, a ``MockTransport`` answers by ``Host``);
- the model is a scripted ``FakeModelGateway``; runs use the Postgres run store.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import uvicorn
from app.core.config import Settings
from app.core.correlation import CorrelationMiddleware
from app.core.ports.model_gateway import ModelEvent, ModelGatewayError
from app.modules.auth.principal import AuthenticationFailure
from app.modules.auth.router import auth_error_handler
from app.modules.memory import MemoryCipher, MemoryService
from app.modules.models.vault import KeyVault
from app.modules.oauth.cimd import ClientMetadataResolver
from app.modules.oauth.composition import OAuthStack, build_oauth_stack, mount_oauth
from app.modules.oauth.service import OAuthConfig
from app.modules.runs import EngineConfig, RunEngine, RunNotifier, RunService, SqlRunStore
from app.modules.tools.router import McpToolRouter
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes import FakeModelGateway
from tests.memory.conftest import KEYRING
from tests.runs.conftest import TEST_LIMITS, _migrated_factory, create_users

ISSUER = "https://mia.test"
CONSENT = "https://mia.test/oauth.html"
CLIENT_ID = "https://notes.example/oauth/client.json"
REDIRECT = "https://notes.example/callback"
PUBLIC_IP = "93.184.216.34"


def client_document(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_id": CLIENT_ID,
        "client_name": "Notes",
        "client_uri": "https://notes.example",
        "redirect_uris": [REDIRECT, "http://127.0.0.1:8765/cb"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    body.update(overrides)
    return body


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


class FirstParty:
    async def authenticate(self, request: Request) -> SimpleNamespace:
        user = request.headers.get("x-test-user")
        if not user:
            raise AuthenticationFailure(
                401, "authentication_required", "Authentication is required."
            )
        return SimpleNamespace(user_id=user)


@dataclass
class Documents:
    """Client metadata documents keyed by URL (served by ``Host`` + path)."""

    bodies: dict[str, Any] = field(default_factory=dict)
    fetches: list[str] = field(default_factory=list)

    def transport(self) -> httpx2.MockTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            host = request.headers.get("host", "")
            url = f"https://{host}{request.url.path}"
            self.fetches.append(url)
            body = self.bodies.get(url)
            if body is None:
                return httpx2.Response(404)
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            return httpx2.Response(200, content=raw, headers={"content-type": "application/json"})

        return httpx2.MockTransport(handle)


async def public_resolver(host: str, port: int) -> Sequence[str]:
    return [PUBLIC_IP]


@dataclass
class OAuthWorld:
    app: FastAPI
    stack: OAuthStack
    factory: async_sessionmaker[AsyncSession]
    users: list[uuid.UUID]
    gateway: FakeModelGateway
    memory: MemoryService
    runs: RunService
    documents: Documents

    @property
    def user(self) -> uuid.UUID:
        return self.users[0]

    def http(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=ISSUER, **kwargs
        )

    def script(self, *steps: Sequence[ModelEvent] | ModelGatewayError) -> None:
        for step in steps:
            self.gateway.add_step(step)

    async def authorize(
        self,
        http: httpx.AsyncClient,
        *,
        scope: str = "openid profile email llm.generate",
        challenge: str,
        state: str = "st-123",
        redirect_uri: str = REDIRECT,
        client_id: str = CLIENT_ID,
        extra: dict[str, str] | None = None,
    ) -> httpx.Response:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **(extra or {}),
        }
        return await http.get("/oauth/authorize", params=params)

    async def consent(
        self,
        http: httpx.AsyncClient,
        location: str,
        *,
        user: uuid.UUID | None = None,
        body: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> httpx.Response:
        handle = parse_qs(urlsplit(location).query)["request"][0]
        headers = {"x-test-user": str(user or self.user)}
        view = await http.get(f"/v1/oauth/requests/{handle}", headers=headers)
        assert view.status_code == 200, view.text
        return await http.post(
            f"/v1/oauth/requests/{handle}/decision",
            json=body or {"decision": "approve"},
            headers={**headers, "Idempotency-Key": key or uuid.uuid4().hex},
        )

    async def login(
        self,
        http: httpx.AsyncClient,
        *,
        scope: str = "openid profile email llm.generate",
        user: uuid.UUID | None = None,
        decision: dict[str, Any] | None = None,
        resource: str | None = None,
    ) -> dict[str, Any]:
        """Full authorization code + PKCE flow; returns the token response."""
        verifier, challenge = pkce_pair()
        extra = {"resource": resource} if resource else None
        started = await self.authorize(http, scope=scope, challenge=challenge, extra=extra)
        assert started.status_code == 302, started.text
        decided = await self.consent(http, started.headers["location"], user=user, body=decision)
        assert decided.status_code == 200, decided.text
        code = parse_qs(urlsplit(decided.json()["redirect_to"]).query)["code"][0]
        token = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT,
            },
        )
        assert token.status_code == 200, token.text
        return dict(token.json())


def settings() -> Settings:
    return Settings(app_env="test", public_base_url=ISSUER)


@asynccontextmanager
async def oauth_world(
    *,
    router_factory: Callable[[list[uuid.UUID]], Awaitable[McpToolRouter]] | None = None,
    approvals: Any = None,
    config: OAuthConfig | None = None,
    documents: Documents | None = None,
) -> AsyncIterator[OAuthWorld]:
    factory = await _migrated_factory()
    users = await create_users(factory, 2)
    gateway = FakeModelGateway()
    memory = MemoryService(factory, MemoryCipher(KeyVault(KEYRING)))
    router = await router_factory(users) if router_factory else None
    if approvals is None and router is not None:
        approvals = router._approvals  # the router's own store: approvals it creates are waited on
    engine = RunEngine(
        store=SqlRunStore(factory),
        notifier=RunNotifier(),
        gateway=gateway,
        config=EngineConfig(limits=TEST_LIMITS),
        router=router,
        approvals=approvals,
    )
    runs = RunService(engine, keepalive_s=0.2)
    docs = documents or Documents({CLIENT_ID: client_document()})
    clients = ClientMetadataResolver(
        resolver=public_resolver, http_transport=docs.transport, cache_s=60
    )
    stack = build_oauth_stack(
        settings(),
        factory,
        runs=runs,
        router=router,
        approvals=approvals,
        memory=memory,
        clients=clients,
        config=config or OAuthConfig(issuer=ISSUER, consent_url=CONSENT),
    )
    app = FastAPI()
    app.add_exception_handler(AuthenticationFailure, auth_error_handler)
    mount_oauth(app, stack, first_party=FirstParty(), memory=memory)
    app.add_middleware(CorrelationMiddleware)
    # The MCP session manager owns an anyio task group: enter and exit it in one task.
    started, stop = asyncio.Event(), asyncio.Event()

    async def keeper() -> None:
        async with stack.mcp.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(keeper())
    await started.wait()
    try:
        yield OAuthWorld(app, stack, factory, users, gateway, memory, runs, docs)
    finally:
        stop.set()
        await task
    await runs.aclose()
    await factory.kw["bind"].dispose()


@contextmanager
def served_world(port: int) -> Iterator[tuple[OAuthWorld, str]]:
    """The world under uvicorn on 127.0.0.1:<port> in its own thread and event loop.

    Yields the world and an access token (``openid profile email llm.generate``).
    """
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def run() -> None:
        async def main() -> None:
            async with oauth_world() as world:
                async with world.http() as http:
                    holder["token"] = (await world.login(http))["access_token"]
                server = uvicorn.Server(
                    uvicorn.Config(
                        world.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
                    )
                )
                holder.update(world=world, server=server)
                task = asyncio.create_task(server.serve())
                while not server.started and not task.done():  # noqa: ASYNC110
                    await asyncio.sleep(0.05)
                ready.set()
                await task

        try:
            asyncio.run(main())
        finally:
            ready.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(60) and "server" in holder, "uvicorn did not start"
    try:
        yield holder["world"], holder["token"]
    finally:
        holder["server"].should_exit = True
        thread.join(30)
