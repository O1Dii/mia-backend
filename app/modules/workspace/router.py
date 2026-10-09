"""`/v1/workspace`, `/v1/installations*` and `/v1/dashboard*` routes.

Authentication is the shared :class:`RequestAuthenticator`: Android bearer and
web cookie reach the same workspace.
"""

from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from app.modules.auth.principal import RequestAuthenticator
from app.modules.workspace.errors import WorkspaceUnavailableError
from app.modules.workspace.schemas import (
    CatalogInstallRequest,
    CustomInstallRequest,
    DashboardView,
    InstallationList,
    InstallationView,
    PatchInstallationRequest,
    PatchTileRequest,
    PluginStateView,
    PutDashboardRequest,
    PutPluginStateRequest,
    WorkspaceView,
)
from app.modules.workspace.service import MutationResult, WorkspaceContext, WorkspaceService

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]


def create_workspace_router(
    service: WorkspaceService, authenticator: RequestAuthenticator
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["Plugins"])

    async def workspace_context(request: Request) -> WorkspaceContext:
        principal = await authenticator.authenticate(request)
        try:
            return await service.context_for(principal.user_id)
        except SQLAlchemyError as exc:
            raise WorkspaceUnavailableError("The workspace is temporarily unavailable.") from exc

    Context = Annotated[WorkspaceContext, Depends(workspace_context)]

    @router.get("/workspace", response_model=WorkspaceView)
    async def get_workspace(ctx: Context) -> Any:
        return await _guard(service.get_workspace(ctx))

    @router.get("/installations", response_model=InstallationList)
    async def list_installations(ctx: Context) -> Any:
        return InstallationList(items=await _guard(service.list_installations(ctx)))

    @router.post("/installations", status_code=201, response_model=InstallationView)
    async def create_installation(
        ctx: Context,
        idempotency_key: IdempotencyKey,
        payload: Annotated[CatalogInstallRequest | CustomInstallRequest, Body()],
    ) -> Response:
        return _respond(
            await _guard(service.create_installation(ctx, payload, idempotency_key=idempotency_key))
        )

    @router.get("/installations/{installation_id}", response_model=InstallationView)
    async def get_installation(ctx: Context, installation_id: UUID) -> Any:
        return await _guard(service.get_installation(ctx, installation_id))

    @router.patch("/installations/{installation_id}", response_model=InstallationView)
    async def update_installation(
        ctx: Context,
        installation_id: UUID,
        payload: PatchInstallationRequest,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _respond(
            await _guard(
                service.update_installation(
                    ctx, installation_id, payload, idempotency_key=idempotency_key
                )
            )
        )

    @router.delete("/installations/{installation_id}", status_code=204)
    async def delete_installation(
        ctx: Context,
        installation_id: UUID,
        idempotency_key: IdempotencyKey,
        delete_data: Annotated[bool, Query()] = False,
    ) -> Response:
        return _respond(
            await _guard(
                service.delete_installation(
                    ctx,
                    installation_id,
                    delete_data=delete_data,
                    idempotency_key=idempotency_key,
                )
            )
        )

    @router.get("/installations/{installation_id}/state", response_model=PluginStateView)
    async def get_plugin_state(ctx: Context, installation_id: UUID) -> Any:
        return await _guard(service.get_plugin_state(ctx, installation_id))

    @router.put("/installations/{installation_id}/state", response_model=PluginStateView)
    async def put_plugin_state(
        ctx: Context,
        installation_id: UUID,
        payload: PutPluginStateRequest,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _respond(
            await _guard(
                service.put_plugin_state(
                    ctx, installation_id, payload, idempotency_key=idempotency_key
                )
            )
        )

    @router.get("/dashboard", response_model=DashboardView)
    async def get_dashboard(ctx: Context) -> Any:
        return await _guard(service.get_dashboard(ctx))

    @router.put("/dashboard", response_model=DashboardView)
    async def put_dashboard(
        ctx: Context, payload: PutDashboardRequest, idempotency_key: IdempotencyKey
    ) -> Response:
        return _respond(
            await _guard(service.put_dashboard(ctx, payload, idempotency_key=idempotency_key))
        )

    @router.patch("/dashboard/tiles/{tile_id}")
    async def patch_tile(
        ctx: Context, tile_id: UUID, payload: PatchTileRequest, idempotency_key: IdempotencyKey
    ) -> Response:
        return _respond(
            await _guard(service.patch_tile(ctx, tile_id, payload, idempotency_key=idempotency_key))
        )

    return router


async def _guard[T](operation: Awaitable[T]) -> T:
    try:
        result = await operation
    except SQLAlchemyError as exc:
        raise WorkspaceUnavailableError("The workspace is temporarily unavailable.") from exc
    return result


def _respond(result: MutationResult) -> Response:
    if result.body is None:
        return Response(status_code=result.status_code)
    return JSONResponse(status_code=result.status_code, content=result.body)
