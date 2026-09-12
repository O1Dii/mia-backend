"""FastAPI application entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.api.errors import service_unavailable
from app.composition import install_runtime_api
from app.core.config import Settings, get_settings
from app.core.database import Database
from app.core.logging import configure_logging
from app.core.redis import RedisClient
from app.core.runtime import Runtime

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None, *, install_modules: bool = True
) -> FastAPI:
    """Build the application without connecting to external dependencies at import time."""
    resolved_settings = settings or get_settings()
    runtime = Runtime(
        database=Database(resolved_settings.database_url),
        redis=RedisClient(resolved_settings.redis_url),
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
    if install_modules:
        install_runtime_api(app, resolved_settings, runtime)

    @app.middleware("http")
    async def add_request_id(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
            },
        )
        return response

    @app.get("/health/live", tags=["Health"])
    async def live() -> dict[str, str]:
        """Return liveness without checking network dependencies."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"], response_model=None)
    async def ready(request: Request) -> JSONResponse | dict[str, str]:
        """Return readiness only after PostgreSQL and Redis respond."""
        runtime: Runtime = request.app.state.runtime
        if await runtime.is_ready():
            return {"status": "ok"}
        return service_unavailable(str(request.state.request_id))

    return app


app = create_app()
