"""Wiring for the Model Gateway: keyring, HTTP client, services and the API router.

Consumers (M12/M17/M50/M61) get the gateway from ``app.state.model_gateway`` or by calling
``build_models_stack`` themselves in tests (inject ``transport`` to replay recordings).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.crypto import MasterKeyring
from app.core.runtime import Runtime
from app.modules.models.adapters import AdapterSettings
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.chatgpt_plan.credentials import PlanCredentialStore
from app.modules.models.chatgpt_plan.jwks import JwksVerifier
from app.modules.models.chatgpt_plan.signin import SignInCoordinator
from app.modules.models.connections import ConnectionService
from app.modules.models.gateway import DefaultModelGateway
from app.modules.models.router import Authenticator, build_models_router
from app.modules.models.vault import KeyVault

_DEV_KEY_ID = "dev-local"


def build_keyring(settings: Settings) -> MasterKeyring:
    """Master keys from ``CREDENTIALS_MASTER_KEYS``; a fixed local-only key in dev/test.

    Staging/production refuse to start without configured keys (see ``Settings``).
    """
    if settings.credentials_master_keys is not None:
        return MasterKeyring.from_config(
            settings.credentials_master_keys.get_secret_value(),
            settings.credentials_master_key_active,
        )
    seed = hashlib.sha256(b"mia-local-development-master-key-not-for-production").digest()
    return MasterKeyring({_DEV_KEY_ID: seed}, _DEV_KEY_ID)


@dataclass(slots=True)
class ModelsStack:
    gateway: DefaultModelGateway
    connections: ConnectionService
    vault: KeyVault
    plan_store: PlanCredentialStore
    sign_in: SignInCoordinator
    client: httpx.AsyncClient

    async def aclose(self) -> None:
        await self.sign_in.aclose()
        await self.client.aclose()


def build_models_stack(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    keyring: MasterKeyring | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    adapter_settings: AdapterSettings | None = None,
) -> ModelsStack:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.model_request_timeout_seconds, connect=5.0),
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    )
    vault = KeyVault(keyring or build_keyring(settings))
    connections = ConnectionService(session_factory, vault)
    plan_store = PlanCredentialStore(session_factory, vault, client)
    verifier = JwksVerifier(client, issuer=oauth.ISSUER)
    sign_in = SignInCoordinator(
        session_factory=session_factory, store=plan_store, client=client, verifier=verifier
    )
    gateway = DefaultModelGateway(
        connections,
        client,
        adapter_settings
        or AdapterSettings(
            stream_deadline_seconds=settings.model_stream_deadline_seconds,
            pause_seconds=settings.model_usage_pause_seconds,
        ),
        plan_store=plan_store,
    )
    return ModelsStack(gateway, connections, vault, plan_store, sign_in, client)


def install_models(
    app: FastAPI,
    settings: Settings,
    runtime: Runtime,
    authenticator: Authenticator,
) -> ModelsStack:
    """Mount ``/v1/model-connections`` and expose the gateway on ``app.state``."""
    stack = build_models_stack(settings, runtime.database.session_factory)
    app.include_router(
        build_models_router(
            authenticator=authenticator,
            connections=stack.connections,
            gateway=stack.gateway,
            plan_store=stack.plan_store,
            sign_in=stack.sign_in,
        )
    )
    app.state.model_gateway = stack.gateway
    app.state.models_stack = stack

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with original(application):
            try:
                yield
            finally:
                await stack.aclose()

    app.router.lifespan_context = lifespan
    return stack
