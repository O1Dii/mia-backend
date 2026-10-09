"""Store, installs and account connections (contracts/mvp/v1/openapi.yaml, Review §5.4.5).

Authentication is the shared :class:`RequestAuthenticator` (Android bearer or web cookie),
except the two public documents the OAuth flow needs: the provider redirect
``GET /v1/connections/callback`` (the single-use ``state`` is its credential) and the Client
ID Metadata Document. Publishing is not an HTTP operation: administrators run
``python -m cli.mia_plugin publish <dir>`` on the backend host.
"""

import hashlib
import html
from collections.abc import Awaitable
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.modules.auth.principal import RequestAuthenticator
from app.modules.connections.oauth.service import ConnectionInfo, ConnectionService
from app.modules.plugins.catalog.package import media_type
from app.modules.plugins.catalog.schemas import (
    CatalogEntryDetail,
    CatalogPage,
    ConsentCustomRequest,
    ConsentView,
    DiscoverResult,
    InstallRequest,
    PutReviewRequest,
    ReviewList,
    UpdateInfo,
    UpgradeRequest,
)
from app.modules.plugins.catalog.service import CatalogService, consent_of
from app.modules.plugins.install.service import InstallService
from app.modules.workspace.errors import (
    ResourceNotFoundError,
    WorkspaceUnavailableError,
)
from app.modules.workspace.schemas import PLUGIN_ID_PATTERN, InstallationView
from app.modules.workspace.service import MutationResult, WorkspaceContext, WorkspaceService

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
PluginId = Annotated[str, Query(pattern=PLUGIN_ID_PATTERN)]
BINDING_COOKIE = "mia_oauth_binding"
"""Lax cookie that ties an OAuth callback to the browser that started the flow (OI-M15-003)."""
_IMAGE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "public, max-age=3600, immutable",
}


class StartConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: UUID
    return_to: str | None = Field(default=None, max_length=300)
    """Relative path on this origin the browser is sent to after the provider's consent."""

    @field_validator("return_to")
    @classmethod
    def relative_path(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or any(char in value for char in "\r\n\t ")
        ):
            raise ValueError("return_to must be a path on this origin")
        return value


class ConnectionView(BaseModel):
    id: UUID
    installation_id: UUID
    plugin_id: str
    status: str
    account_label: str | None
    scopes: str | None


class ConnectionList(BaseModel):
    items: list[ConnectionView]


def build_plugins_router(
    *,
    catalog: CatalogService,
    installs: InstallService,
    connections: ConnectionService,
    workspace: WorkspaceService,
    authenticator: RequestAuthenticator,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["Plugins"])
    # The browser sees the proxy prefix (M62-T1): the binding cookie must name the full path.
    callback_cookie_path = authenticator.policy.base_path + "/v1/connections/callback"

    async def workspace_context(request: Request) -> WorkspaceContext:
        principal = await authenticator.authenticate(request)
        try:
            return await workspace.context_for(principal.user_id)
        except SQLAlchemyError as exc:
            raise WorkspaceUnavailableError("The workspace is temporarily unavailable.") from exc

    Context = Annotated[WorkspaceContext, Depends(workspace_context)]

    # ---------------------------------------------------------------- catalog

    @router.get("/plugins", response_model=CatalogPage)
    async def list_catalog(
        ctx: Context,
        query: Annotated[str | None, Query(max_length=100)] = None,
        category: Annotated[str | None, Query(max_length=24)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    ) -> Any:
        return await _guard(
            catalog.list_entries(query=query, category=category, limit=limit, cursor=cursor)
        )

    @router.get("/plugins/{plugin_id}", response_model=CatalogEntryDetail)
    async def get_catalog_entry(
        ctx: Context, plugin_id: str, version: Annotated[str | None, Query(max_length=64)] = None
    ) -> Any:
        return await _guard(catalog.entry(plugin_id, version))

    @router.get("/plugins/{plugin_id}/consent", response_model=ConsentView)
    async def get_consent(
        ctx: Context, plugin_id: str, version: Annotated[str | None, Query(max_length=64)] = None
    ) -> Any:
        return consent_of(await _guard(catalog.release(plugin_id, version)))

    @router.get("/plugins/{plugin_id}/releases/{version}/files/{path:path}")
    async def get_release_file(ctx: Context, plugin_id: str, version: str, path: str) -> Response:
        file = await _guard(catalog.file(plugin_id, version, path))
        if file is None:
            raise ResourceNotFoundError("The file was not found.")
        return Response(content=file.content, media_type=media_type(path), headers=_IMAGE_HEADERS)

    @router.get("/plugins/{plugin_id}/reviews", response_model=ReviewList)
    async def list_reviews(ctx: Context, plugin_id: str) -> Any:
        return await _guard(catalog.reviews(ctx.user_id, plugin_id))

    @router.put("/plugins/{plugin_id}/reviews/me", status_code=204)
    async def put_review(
        ctx: Context, plugin_id: str, payload: PutReviewRequest, idempotency_key: IdempotencyKey
    ) -> Response:
        await _guard(catalog.release(plugin_id))
        await _guard(workspace.upsert_review(ctx.user_id, plugin_id, payload.rating, payload.text))
        return Response(status_code=204)

    @router.delete("/plugins/{plugin_id}/reviews/me", status_code=204)
    async def delete_review(
        ctx: Context, plugin_id: str, idempotency_key: IdempotencyKey
    ) -> Response:
        await _guard(workspace.delete_review(ctx.user_id, plugin_id))
        return Response(status_code=204)

    # ---------------------------------------------------------------- installs

    @router.post("/plugins/{plugin_id}/install", status_code=201, response_model=InstallationView)
    async def install_plugin(
        ctx: Context, plugin_id: str, payload: InstallRequest, idempotency_key: IdempotencyKey
    ) -> Response:
        return _respond(
            await _guard(installs.install(ctx, plugin_id, payload, idempotency_key=idempotency_key))
        )

    @router.get("/installations/{installation_id}/update", response_model=UpdateInfo)
    async def get_update(ctx: Context, installation_id: UUID) -> Any:
        return await _guard(installs.update_info(ctx, installation_id))

    @router.post("/installations/{installation_id}/upgrade", response_model=InstallationView)
    async def upgrade_installation(
        ctx: Context,
        installation_id: UUID,
        payload: UpgradeRequest,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _respond(
            await _guard(
                installs.upgrade(ctx, installation_id, payload, idempotency_key=idempotency_key)
            )
        )

    @router.get("/installations/{installation_id}/discovery", response_model=DiscoverResult)
    async def discover_custom(ctx: Context, installation_id: UUID) -> Any:
        return await _guard(installs.discover_custom(ctx, installation_id))

    @router.post("/installations/{installation_id}/consent", response_model=InstallationView)
    async def consent_custom(
        ctx: Context,
        installation_id: UUID,
        payload: ConsentCustomRequest,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _respond(
            await _guard(
                installs.consent_custom(
                    ctx, installation_id, payload, idempotency_key=idempotency_key
                )
            )
        )

    # ------------------------------------------------------------ connections

    @router.get("/connections", response_model=ConnectionList)
    async def list_connections(ctx: Context) -> Any:
        return ConnectionList(
            items=[
                _connection_view(info) for info in await _guard(connections.list_connections(ctx))
            ]
        )

    @router.post("/connections", status_code=201)
    async def start_connection(
        ctx: Context,
        request: Request,
        payload: StartConnectionRequest,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        # A browser (session cookie, no bearer) gets a binding; the Android app has no cookie jar
        # shared with the system browser, so its flows stay unbound (tracked as OI-M30-002).
        web = request.headers.get("authorization") is None
        binding = (
            hashlib.sha256(
                f"mia-oauth-binding:{ctx.user_id}:{idempotency_key}".encode()
            ).hexdigest()
            if web
            else None
        )
        response = _respond(
            await _guard(
                connections.start(
                    ctx,
                    payload.installation_id,
                    return_to=payload.return_to,
                    idempotency_key=idempotency_key,
                    binding=binding,
                )
            )
        )
        if binding is not None and response.status_code < 300:
            response.set_cookie(
                BINDING_COOKIE,
                binding,
                max_age=connections.flow_ttl_seconds,
                path=callback_cookie_path,
                httponly=True,
                samesite="lax",
                secure=authenticator.policy.secure,
            )
        return response

    @router.get("/connections/callback", include_in_schema=False)
    async def oauth_callback(
        request: Request,
        state: Annotated[str | None, Query(max_length=256)] = None,
        code: Annotated[str | None, Query(max_length=2048)] = None,
        error: Annotated[str | None, Query(max_length=128)] = None,
    ) -> Response:
        outcome = await _guard(
            connections.complete(
                state=state, code=code, error=error, binding=request.cookies.get(BINDING_COOKIE)
            )
        )
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        result: Response
        if outcome.return_to is not None:
            query = urlencode(
                {
                    "connection": "ok" if outcome.ok else "error",
                    **({"code": outcome.code} if outcome.code else {}),
                }
            )
            separator = "&" if "?" in outcome.return_to else "?"
            result = RedirectResponse(
                authenticator.policy.base_path + outcome.return_to + separator + query,
                status_code=303,
                headers=headers,
            )
        else:
            title = "Аккаунт подключён" if outcome.ok else "Не удалось подключить аккаунт"
            body = (
                "Можно вернуться в Mia." if outcome.ok else "Вернитесь в Mia и попробуйте ещё раз."
            )
            result = HTMLResponse(
                "<!doctype html><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width'>"
                f"<title>{html.escape(title)}</title><h1>{html.escape(title)}</h1>"
                f"<p>{html.escape(body)}</p>",
                status_code=200 if outcome.ok else 400,
                headers=headers,
            )
        result.delete_cookie(BINDING_COOKIE, path=callback_cookie_path)
        return result

    @router.get("/connections/client-metadata.json", include_in_schema=False)
    async def client_metadata() -> Response:
        return JSONResponse(
            connections.client_metadata(), headers={"Cache-Control": "max-age=3600"}
        )

    @router.delete("/connections/{connection_id}", status_code=204)
    async def delete_connection(
        ctx: Context, connection_id: UUID, idempotency_key: IdempotencyKey
    ) -> Response:
        return _respond(
            await _guard(
                connections.disconnect(ctx, connection_id, idempotency_key=idempotency_key)
            )
        )

    return router


def _connection_view(info: ConnectionInfo) -> ConnectionView:
    return ConnectionView(
        id=info.id,
        installation_id=info.installation_id,
        plugin_id=info.plugin_id,
        status=info.status,
        account_label=info.account_label,
        scopes=info.scopes,
    )


async def _guard[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except SQLAlchemyError as exc:
        raise WorkspaceUnavailableError("The store is temporarily unavailable.") from exc


def _respond(result: MutationResult) -> Response:
    if result.body is None:
        return Response(status_code=result.status_code)
    return JSONResponse(status_code=result.status_code, content=result.body)
