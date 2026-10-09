"""Application-level composition for the versioned MIA API."""

from fastapi import FastAPI
from pydantic import SecretStr

from app.api.v1.device_tools import install_device_tools
from app.api.v1.feed import build_feed_router
from app.core.circuit import GuardedRealtimeSession, GuardedTextProvider, ProviderCircuit
from app.core.config import Settings
from app.core.limits import DailyBudget
from app.core.runtime import Runtime
from app.modules.auth.composition import install_auth, request_authenticator
from app.modules.auth.service import AuthService, InvalidCredentialsError
from app.modules.auth.tokens import InvalidAccessTokenError
from app.modules.conversations.feed import FeedService
from app.modules.conversations.router import build_conversation_router
from app.modules.conversations.streaming import TextSSEService
from app.modules.jobs.composition import install_jobs
from app.modules.memory import install_memory
from app.modules.models import install_models
from app.modules.plugins.composition import build_builtin_plugins
from app.modules.plugins.install.composition import (
    build_catalog_stack,
    build_connections,
    install_store,
)
from app.modules.realtime import (
    AuthenticatedUser,
    FakeRealtimeProvider,
    InMemoryRealtimeSessionStore,
    RealtimeGateway,
    RealtimeSession,
    build_realtime_router,
)
from app.modules.realtime.provider import RealtimeProviderFactory, RealtimeProviderSession
from app.modules.runs.composition import install_runs
from app.modules.tools import ToolExecutor, ToolRegistry, current_datetime_registry
from app.modules.tools.composition import install_tools
from app.modules.workspace.composition import install_workspace
from app.providers.ai.base import TextAIProvider
from app.providers.ai.fake import FakeAIProvider
from app.providers.ai.gemini_live import GeminiLiveAdapterFactory, GeminiLiveConfig
from app.providers.ai.gemini_text import GeminiTextAIProvider, GeminiTextConfig


class AuthServiceAuthenticator:
    """Expose one S12 bearer verifier to conversation and realtime modules."""

    def __init__(self, service: AuthService) -> None:
        self._service = service

    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None:
        try:
            account = await self._service.authenticate_access(bearer_token)
        except (InvalidAccessTokenError, InvalidCredentialsError):
            return None
        return AuthenticatedUser(user_id=account.principal.user_id)


def _require_gemini_settings(settings: Settings) -> tuple[SecretStr, str, str]:
    if (
        settings.gemini_api_key is None
        or settings.gemini_text_model is None
        or settings.gemini_live_model is None
    ):
        raise RuntimeError("Gemini provider configuration is incomplete")
    return (
        settings.gemini_api_key,
        settings.gemini_text_model,
        settings.gemini_live_model,
    )


def _text_provider(settings: Settings) -> TextAIProvider:
    if settings.ai_provider == "fake":
        return FakeAIProvider()
    api_key, text_model, _ = _require_gemini_settings(settings)
    provider = GeminiTextAIProvider(
        GeminiTextConfig(
            api_key=api_key,
            model=text_model,
            request_timeout_seconds=settings.gemini_request_timeout_seconds,
        )
    )
    return GuardedTextProvider(
        provider,
        ProviderCircuit(
            settings.provider_failure_threshold,
            settings.provider_cooldown_seconds,
            name="gemini_text",
        ),
    )


def _fake_realtime_provider(session: RealtimeSession) -> RealtimeProviderSession:
    return FakeRealtimeProvider(session)


def _realtime_provider_factory(
    settings: Settings, registry: ToolRegistry, circuit: ProviderCircuit | None = None
) -> RealtimeProviderFactory:
    if settings.ai_provider == "fake":
        return _fake_realtime_provider
    api_key, _, live_model = _require_gemini_settings(settings)
    factory = GeminiLiveAdapterFactory(
        GeminiLiveConfig(
            api_key=api_key,
            model=live_model,
            system_instruction=TextSSEService.STABLE_INSTRUCTION,
            request_timeout_seconds=settings.gemini_request_timeout_seconds,
            tools=registry.declarations(),
        )
    )
    if circuit is None:
        return factory
    guard = circuit

    def guarded_factory(session: RealtimeSession) -> RealtimeProviderSession:
        return GuardedRealtimeSession(factory(session), guard)

    return guarded_factory


def install_runtime_api(app: FastAPI, settings: Settings, runtime: Runtime) -> None:
    """Mount W3 auth, conversation, and realtime modules on one runtime."""
    auth_service = install_auth(
        app,
        settings,
        database=runtime.database,
        redis=runtime.redis,
    )
    authenticator = AuthServiceAuthenticator(auth_service)
    models_stack = install_models(app, settings, runtime, authenticator)
    builtin_plugins = build_builtin_plugins(runtime, model_gateway=models_stack.gateway)
    catalog_stack = build_catalog_stack(settings, runtime, builtin_plugins)
    workspace = install_workspace(app, database=runtime.database, catalog=catalog_stack.catalog)
    connections = build_connections(
        settings, runtime, vault=models_stack.vault, workspace=workspace
    )
    text_provider = _text_provider(settings)
    registry = current_datetime_registry()
    tool_executor = ToolExecutor(runtime.database.session_factory, registry)
    budget = DailyBudget(runtime.redis)
    tools = install_tools(
        app,
        settings,
        runtime,
        server_factory=builtin_plugins.server_factory,
        manifest_source=catalog_stack.manifest_source,
        credentials=connections,
    )
    install_store(
        app,
        settings,
        runtime,
        catalog=catalog_stack,
        connections=connections,
        tools=tools,
        workspace=workspace,
    )
    memory = install_memory(app, runtime, vault=models_stack.vault, tools=tools)
    runs = install_runs(
        app,
        settings,
        runtime,
        authenticator=authenticator,
        model_gateway=models_stack.gateway,
        tool_router=tools.router,
        approvals=tools.approvals,
        memory=memory.service,
    )
    tools.device_channel.bind_publisher(runs.publish)
    install_jobs(app, settings, runtime, runs=runs, core_tools=tools.core_tools)
    app.include_router(
        build_feed_router(
            service=FeedService(runtime.database.session_factory),
            authenticator=request_authenticator(app),
        )
    )
    memory.tools.bind_publisher(runs.publish)
    install_device_tools(
        app,
        channel=tools.device_channel,
        runs=runs,
        authenticator=request_authenticator(app),
    )
    app.include_router(
        build_conversation_router(
            session_factory=runtime.database.session_factory,
            authenticator=authenticator,
            stream_service=TextSSEService(
                runtime.database.session_factory, text_provider, tool_executor=tool_executor
            ),
            budget=budget,
            max_daily_turns=settings.max_daily_text_turns,
            text_enabled=settings.text_enabled,
        )
    )

    live_circuit = (
        ProviderCircuit(
            settings.provider_failure_threshold,
            settings.provider_cooldown_seconds,
            name="gemini_live",
        )
        if settings.ai_provider == "gemini"
        else None
    )
    realtime_store = InMemoryRealtimeSessionStore(
        max_active_per_user=settings.max_active_live_sessions_per_user
    )
    realtime_gateway = RealtimeGateway(
        store=realtime_store,
        authenticator=authenticator,
        provider_factory=_realtime_provider_factory(settings, registry, live_circuit),
        tool_executor=tool_executor,
        max_session_seconds=settings.max_live_session_seconds,
    )
    app.include_router(
        build_realtime_router(
            store=realtime_store,
            authenticator=authenticator,
            gateway=realtime_gateway,
            budget=budget,
            max_daily_sessions=settings.max_daily_live_sessions,
            voice_enabled=settings.voice_enabled,
            provider_available=live_circuit.available if live_circuit is not None else None,
        )
    )
