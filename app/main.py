"""FastAPI application entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.errors import internal_error, service_unavailable
from app.composition import install_runtime_api
from app.core.config import Settings, get_settings
from app.core.correlation import CorrelationMiddleware
from app.core.database import Database, migration_head
from app.core.logging import configure_logging
from app.core.metrics import METRICS
from app.core.redis import RedisClient
from app.core.runtime import Runtime

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, install_modules: bool = True) -> FastAPI:
    """Build the application without connecting to external dependencies at import time."""
    resolved_settings = settings or get_settings()
    runtime = Runtime(
        database=Database(resolved_settings.database_url),
        redis=RedisClient(resolved_settings.redis_url),
        expected_schema_revision=migration_head(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings.log_level)
        logger.info(
            "application_started",
            extra={
                "environment": resolved_settings.app_env,
                "ai_provider": resolved_settings.ai_provider,
                "gemini_api_key_configured": resolved_settings.has_gemini_api_key,
            },
        )
        yield
        await app.state.runtime.close()
        logger.info("application_stopped")

    app = FastAPI(title="MIA Backend", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.add_exception_handler(Exception, internal_error)
    app.add_middleware(CorrelationMiddleware)
    if install_modules:
        install_runtime_api(app, resolved_settings, runtime)

    @app.get("/health/live", tags=["Health"])
    async def live() -> dict[str, str]:
        """Return liveness without checking network dependencies."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"], response_model=None)
    async def ready(request: Request) -> JSONResponse | dict[str, str]:
        """Return readiness only after PostgreSQL at the schema head and Redis respond."""
        runtime: Runtime = request.app.state.runtime
        if await runtime.is_ready():
            return {"status": "ok"}
        return service_unavailable(str(request.state.request_id))

    if resolved_settings.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> PlainTextResponse:
            """Expose content-free operational metrics to the private scrape network."""
            return PlainTextResponse(
                METRICS.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
            )

    return app


app = create_app()
