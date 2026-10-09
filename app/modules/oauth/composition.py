"""Wiring of «Войти через Mia»: authorization server, app API, ``/mcp``."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.routing import Route

from app.api.mcp_server import MiaMcpEndpoint, MiaMcpTools
from app.api.v1.openai_compat import build_openai_compat_router
from app.core.config import Settings
from app.core.ports.approval_service import ApprovalService
from app.core.ports.tool_router import ToolRouter
from app.modules.memory.service import MemoryService
from app.modules.oauth.apprun import AppRunner
from app.modules.oauth.cimd import ClientMetadataResolver
from app.modules.oauth.errors import AppApiError
from app.modules.oauth.idtoken import IdTokenSigner
from app.modules.oauth.principal import AppAuthenticator
from app.modules.oauth.router import FirstPartyAuthenticator, app_error_handler, build_oauth_router
from app.modules.oauth.service import AuthorizationService, OAuthConfig, ResourcePolicy
from app.modules.oauth.usage import Pricing, UsageLedger
from app.modules.runs.service import RunService


@dataclass
class OAuthStack:
    service: AuthorizationService
    apps: AppAuthenticator
    ledger: UsageLedger
    runner: AppRunner
    mcp: MiaMcpEndpoint


def oauth_config(settings: Settings) -> OAuthConfig:
    base = settings.public_base_url.rstrip("/")
    issuer = (settings.oauth_issuer or base).rstrip("/")
    return OAuthConfig(
        issuer=issuer,
        consent_url=settings.oauth_consent_url or f"{base}/oauth.html",
        access_ttl_s=settings.oauth_access_token_ttl_seconds,
        refresh_ttl_s=settings.oauth_refresh_token_ttl_days * 86400,
        code_ttl_s=settings.oauth_code_ttl_seconds,
        request_ttl_s=settings.oauth_request_ttl_seconds,
        default_monthly_limit_usd=Decimal(str(settings.apps_default_monthly_limit_usd)),
        max_monthly_limit_usd=Decimal(str(settings.apps_max_monthly_limit_usd)),
        extra_resources=frozenset(settings.oauth_extra_resource_list),
        plugin_token_ttl_s=settings.oauth_plugin_token_ttl_seconds,
    )


def build_oauth_stack(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    runs: RunService,
    router: ToolRouter | None = None,
    approvals: ApprovalService | None = None,
    memory: MemoryService | None = None,
    clients: ClientMetadataResolver | None = None,
    config: OAuthConfig | None = None,
    resources: ResourcePolicy | None = None,
) -> OAuthStack:
    secret = settings.auth_jwt_secret.get_secret_value().encode()
    service = AuthorizationService(
        sessions=sessions,
        clients=clients
        or ClientMetadataResolver(
            timeout_s=settings.oauth_cimd_timeout_seconds,
            cache_s=settings.oauth_cimd_cache_seconds,
            allow_loopback=settings.oauth_allow_loopback_clients,
        ),
        signer=IdTokenSigner(secret),
        config=config or oauth_config(settings),
        subject_key=hashlib.sha256(b"mia-oauth-pairwise-sub-v1\n" + secret).digest(),
        resources=resources,
    )
    ledger = UsageLedger(
        sessions,
        Pricing(
            Decimal(str(settings.apps_estimated_input_usd_per_mtok)),
            Decimal(str(settings.apps_estimated_output_usd_per_mtok)),
        ),
    )
    apps = AppAuthenticator(service)
    endpoint = MiaMcpEndpoint(MiaMcpTools(router=router, approvals=approvals, memory=memory), apps)
    return OAuthStack(
        service=service,
        apps=apps,
        ledger=ledger,
        runner=AppRunner(runs=runs, ledger=ledger, memory=memory),
        mcp=endpoint,
    )


def mount_oauth(
    app: FastAPI,
    stack: OAuthStack,
    *,
    first_party: FirstPartyAuthenticator,
    memory: MemoryService | None = None,
) -> OAuthStack:
    app.add_exception_handler(AppApiError, app_error_handler)
    app.include_router(
        build_oauth_router(
            service=stack.service,
            apps=stack.apps,
            first_party=first_party,
            ledger=stack.ledger,
            memory=memory,
        )
    )
    app.include_router(build_openai_compat_router(runner=stack.runner, apps=stack.apps))
    app.router.routes.append(
        Route("/mcp", stack.mcp, methods=["GET", "POST", "DELETE"], include_in_schema=False)
    )
    app.state.oauth = stack

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with original(application), stack.mcp.run():
            yield

    app.router.lifespan_context = lifespan
    return stack


def install_oauth(
    app: FastAPI,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    runs: RunService,
    first_party: FirstPartyAuthenticator,
    router: ToolRouter | None = None,
    approvals: ApprovalService | None = None,
    memory: MemoryService | None = None,
    resources: ResourcePolicy | None = None,
) -> OAuthStack:
    """Mount the AS, ``/v1/chat/completions``, ``/v1/responses``, ``/v1/models`` and ``/mcp``.

    ``resources`` adds plugin servers as RFC 8707 resources (M51-R1, see
    :class:`app.modules.connections.resources.PluginResources`).
    """
    stack = build_oauth_stack(
        settings,
        sessions,
        runs=runs,
        router=router,
        approvals=approvals,
        memory=memory,
        resources=resources,
    )
    return mount_oauth(app, stack, first_party=first_party, memory=memory)
