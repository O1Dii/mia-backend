"""Tenant-scoped workspace service.

Every public method takes a :class:`WorkspaceContext` resolved from the
authenticated user, and every statement filters by ``workspace_id``: a row of
another workspace is indistinguishable from a missing one (404).

Optimistic concurrency: each mutable resource carries a version
(``workspaces.layout_version`` for the dashboard, ``state_version`` for
installations, plugin state and tiles). Mutations take ``expected_version``
and compare-and-swap it in a single ``UPDATE … WHERE version = :expected``;
a stale value raises :class:`VersionConflictError` (HTTP 409 with
``details.current_version``). Successful mutations increment the version by 1.

Idempotency: HTTP mutations carry ``Idempotency-Key``. The key is claimed in the
same transaction as the mutation; a retry with the same key and body replays
the stored response, a different body returns 409 ``idempotency_conflict``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.workspace.catalog import PluginCatalog
from app.modules.workspace.errors import (
    ConflictError,
    IdempotencyConflictError,
    InvalidWorkspaceRequestError,
    ResourceNotFoundError,
    ToolsDigestMismatchError,
    VersionConflictError,
)
from app.modules.workspace.models import (
    Installation,
    PluginState,
    Review,
    WidgetInstance,
    Workspace,
    WorkspaceMutation,
)
from app.modules.workspace.schemas import (
    CatalogInstallRequest,
    CustomInstallRequest,
    DashboardTileView,
    DashboardView,
    InstallationView,
    PatchInstallationRequest,
    PatchTileRequest,
    PluginStateView,
    PutDashboardRequest,
    PutPluginStateRequest,
    TilePlacement,
    WorkspaceView,
)

IDEMPOTENCY_RETENTION = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """Tenant of a request: the string ids used by every core port.

    Build ``ToolContext(user_id=ctx.user_id, workspace_id=ctx.workspace_id, …)``.
    """

    user_id: str
    workspace_id: str

    @property
    def workspace_uuid(self) -> uuid.UUID:
        return uuid.UUID(self.workspace_id)


@dataclass(frozen=True, slots=True)
class MutationResult:
    status_code: int
    body: dict[str, Any] | None


def request_hash(operation: str, target: str, body: Any) -> str:
    canonical = json.dumps(
        {"op": operation, "target": target, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def custom_plugin_id(url: str) -> str:
    return "custom." + hashlib.sha256(url.encode()).hexdigest()[:16]


def installation_view(row: Installation) -> InstallationView:
    return InstallationView(
        id=row.id,
        plugin_id=row.plugin_id,
        version=row.version,
        status=row.status,
        custom_url=row.custom_url,
        source=row.source,
        grants=list(row.grants),
        tools_digest=row.tools_digest,
        state_version=row.state_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def tile_view(row: WidgetInstance) -> DashboardTileView:
    return DashboardTileView(
        id=row.id,
        installation_id=row.installation_id,
        tile_id=row.tile_id,
        size=row.desktop_size,
        position=row.desktop_position,
        mobile=TilePlacement(size=row.mobile_size, position=row.mobile_position),
        settings=dict(row.settings),
        state_version=row.state_version,
    )


class WorkspaceService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        catalog: PluginCatalog,
        *,
        max_installations: int = 60,
    ) -> None:
        self._session_factory = session_factory
        self._catalog = catalog
        self._max_installations = max_installations

    # ----------------------------------------------------------------- context

    async def context_for(self, user_id: str) -> WorkspaceContext:
        """Return (creating on first use) the personal workspace of ``user_id``."""
        owner = uuid.UUID(user_id)
        async with self._session_factory() as session, session.begin():
            workspace_id = await ensure_personal_workspace(session, owner)
        return WorkspaceContext(user_id=str(owner), workspace_id=str(workspace_id))

    async def get_workspace(self, ctx: WorkspaceContext) -> WorkspaceView:
        async with self._session_factory() as session:
            workspace = await self._workspace(session, ctx)
            return WorkspaceView(
                id=workspace.id,
                name=workspace.name,
                owner_user_id=workspace.owner_user_id,
                layout_version=workspace.layout_version,
            )

    # ----------------------------------------------------------- installations

    async def list_installations(self, ctx: WorkspaceContext) -> list[InstallationView]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Installation)
                .where(Installation.workspace_id == ctx.workspace_uuid)
                .order_by(Installation.created_at, Installation.id)
            )
            return [installation_view(row) for row in rows]

    async def get_installation(
        self, ctx: WorkspaceContext, installation_id: uuid.UUID
    ) -> InstallationView:
        async with self._session_factory() as session:
            return installation_view(await self._installation(session, ctx, installation_id))

    async def find_installation(
        self, ctx: WorkspaceContext, plugin_id: str
    ) -> InstallationView | None:
        """Lookup by plugin id for the tool router (M13)."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(Installation).where(
                    Installation.workspace_id == ctx.workspace_uuid,
                    Installation.plugin_id == plugin_id,
                )
            )
            return None if row is None else installation_view(row)

    async def create_installation(
        self,
        ctx: WorkspaceContext,
        payload: CatalogInstallRequest | CustomInstallRequest,
        *,
        idempotency_key: str,
    ) -> MutationResult:
        values: dict[str, Any]
        if isinstance(payload, CatalogInstallRequest):
            plugin = await self._catalog.resolve(payload.plugin_id, payload.version)
            if plugin is None:
                raise ResourceNotFoundError("The plugin version is not in the catalog.")
            if plugin.tools_digest != payload.tools_digest:
                raise ToolsDigestMismatchError(
                    "Tool descriptions changed; review and consent again."
                )
            values = {
                "plugin_id": plugin.plugin_id,
                "version": plugin.version,
                "status": "needs_connection" if plugin.requires_connection else "active",
                "source": "catalog",
                "grants": list(plugin.grants),
                "tools_digest": plugin.tools_digest,
                "custom_url": None,
                "manifest": dict(plugin.manifest) if plugin.manifest is not None else None,
            }
        else:
            url = _validated_custom_url(payload.custom_mcp_url)
            values = {
                "plugin_id": custom_plugin_id(url),
                "version": "0.0.0",
                "status": "needs_consent",
                "source": "custom",
                "grants": [],
                "tools_digest": None,
                "custom_url": url,
                "manifest": None,
            }

        async def work(session: AsyncSession) -> MutationResult:
            existing = await session.scalar(
                select(Installation).where(
                    Installation.workspace_id == ctx.workspace_uuid,
                    Installation.plugin_id == values["plugin_id"],
                )
            )
            if existing is not None:
                if (
                    existing.version == values["version"]
                    and existing.tools_digest == values["tools_digest"]
                ):
                    return MutationResult(201, _dump(installation_view(existing)))
                raise ConflictError("The plugin is already installed in another version.")
            count = await session.scalar(
                select(func.count())
                .select_from(Installation)
                .where(Installation.workspace_id == ctx.workspace_uuid)
            )
            if (count or 0) >= self._max_installations:
                raise ConflictError("The installation limit is reached.")
            row = Installation(workspace_id=ctx.workspace_uuid, **values)
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return MutationResult(201, _dump(installation_view(row)))

        return await self._mutate(
            ctx,
            idempotency_key,
            request_hash("install", "", payload.model_dump(mode="json")),
            work,
        )

    async def update_installation(
        self,
        ctx: WorkspaceContext,
        installation_id: uuid.UUID,
        payload: PatchInstallationRequest,
        *,
        idempotency_key: str,
    ) -> MutationResult:
        changes: dict[str, Any] = {}
        if payload.status is not None:
            changes["status"] = payload.status
        if payload.grants is not None:
            changes["grants"] = sorted(set(payload.grants))

        async def work(session: AsyncSession) -> MutationResult:
            row = await session.scalar(
                update(Installation)
                .where(
                    Installation.id == installation_id,
                    Installation.workspace_id == ctx.workspace_uuid,
                    Installation.state_version == payload.expected_version,
                )
                .values(
                    **changes,
                    state_version=Installation.state_version + 1,
                    updated_at=func.now(),
                )
                .returning(Installation)
            )
            if row is None:
                current = await self._installation(session, ctx, installation_id)
                raise VersionConflictError(current.state_version)
            return MutationResult(200, _dump(installation_view(row)))

        return await self._mutate(
            ctx,
            idempotency_key,
            request_hash(
                "patch-installation", str(installation_id), payload.model_dump(mode="json")
            ),
            work,
        )

    async def delete_installation(
        self,
        ctx: WorkspaceContext,
        installation_id: uuid.UUID,
        *,
        delete_data: bool,
        idempotency_key: str,
    ) -> MutationResult:
        async def work(session: AsyncSession) -> MutationResult:
            tiles = await session.scalar(
                select(func.count())
                .select_from(WidgetInstance)
                .where(
                    WidgetInstance.workspace_id == ctx.workspace_uuid,
                    WidgetInstance.installation_id == installation_id,
                )
            )
            plugin_id = await session.scalar(
                delete(Installation)
                .where(
                    Installation.id == installation_id,
                    Installation.workspace_id == ctx.workspace_uuid,
                )
                .returning(Installation.plugin_id)
            )
            if plugin_id is None:
                raise ResourceNotFoundError("The installation was not found.")
            if tiles:
                # Tiles disappeared through the cascade: invalidate stale layouts.
                await session.execute(
                    update(Workspace)
                    .where(Workspace.id == ctx.workspace_uuid)
                    .values(layout_version=Workspace.layout_version + 1, updated_at=func.now())
                )
            if delete_data:
                await session.execute(
                    delete(PluginState).where(
                        PluginState.workspace_id == ctx.workspace_uuid,
                        PluginState.plugin_id == plugin_id,
                    )
                )
            return MutationResult(204, None)

        return await self._mutate(
            ctx,
            idempotency_key,
            request_hash("uninstall", str(installation_id), {"delete_data": delete_data}),
            work,
        )

    # ------------------------------------------------------------ plugin state

    async def get_plugin_state(
        self, ctx: WorkspaceContext, installation_id: uuid.UUID
    ) -> PluginStateView:
        async with self._session_factory() as session:
            installation = await self._installation(session, ctx, installation_id)
            state = await session.get(PluginState, (ctx.workspace_uuid, installation.plugin_id))
            if state is None:
                return PluginStateView(plugin_id=installation.plugin_id, data={}, state_version=0)
            return PluginStateView(
                plugin_id=state.plugin_id, data=dict(state.data), state_version=state.state_version
            )

    async def put_plugin_state(
        self,
        ctx: WorkspaceContext,
        installation_id: uuid.UUID,
        payload: PutPluginStateRequest,
        *,
        idempotency_key: str,
    ) -> MutationResult:
        async def work(session: AsyncSession) -> MutationResult:
            installation = await self._installation(session, ctx, installation_id)
            plugin_id = installation.plugin_id
            version = await compare_and_set_plugin_state(
                session,
                workspace_id=ctx.workspace_uuid,
                plugin_id=plugin_id,
                data=payload.data,
                expected_version=payload.expected_version,
            )
            view = PluginStateView(plugin_id=plugin_id, data=payload.data, state_version=version)
            return MutationResult(200, _dump(view))

        return await self._mutate(
            ctx,
            idempotency_key,
            request_hash("put-state", str(installation_id), payload.model_dump(mode="json")),
            work,
        )

    # --------------------------------------------------------------- dashboard

    async def get_dashboard(self, ctx: WorkspaceContext) -> DashboardView:
        async with self._session_factory() as session:
            workspace = await self._workspace(session, ctx)
            return await self._dashboard(session, ctx, workspace.layout_version)

    async def put_dashboard(
        self, ctx: WorkspaceContext, payload: PutDashboardRequest, *, idempotency_key: str
    ) -> MutationResult:
        tile_ids = [tile.id for tile in payload.tiles]
        if len(set(tile_ids)) != len(tile_ids):
            raise InvalidWorkspaceRequestError("Tile ids must be unique.")

        async def work(session: AsyncSession) -> MutationResult:
            new_version = await session.scalar(
                update(Workspace)
                .where(
                    Workspace.id == ctx.workspace_uuid,
                    Workspace.layout_version == payload.expected_version,
                )
                .values(layout_version=Workspace.layout_version + 1, updated_at=func.now())
                .returning(Workspace.layout_version)
            )
            if new_version is None:
                workspace = await self._workspace(session, ctx)
                raise VersionConflictError(workspace.layout_version)
            owned_installations = set(
                await session.scalars(
                    select(Installation.id).where(
                        Installation.workspace_id == ctx.workspace_uuid,
                        Installation.id.in_({tile.installation_id for tile in payload.tiles}),
                    )
                )
            )
            if any(tile.installation_id not in owned_installations for tile in payload.tiles):
                raise ResourceNotFoundError("The installation was not found.")
            existing = {
                row.id: row
                for row in await session.scalars(
                    select(WidgetInstance).where(WidgetInstance.workspace_id == ctx.workspace_uuid)
                )
            }
            new_ids = [tile_id for tile_id in tile_ids if tile_id not in existing]
            if new_ids:
                taken = await session.scalar(
                    select(func.count())
                    .select_from(WidgetInstance)
                    .where(WidgetInstance.id.in_(new_ids))
                )
                if taken:
                    raise InvalidWorkspaceRequestError("A tile id is not available.")
            removed = [row_id for row_id in existing if row_id not in set(tile_ids)]
            if removed:
                await session.execute(
                    delete(WidgetInstance).where(
                        WidgetInstance.workspace_id == ctx.workspace_uuid,
                        WidgetInstance.id.in_(removed),
                    )
                )
            for tile in payload.tiles:
                mobile = tile.mobile or TilePlacement(size=tile.size, position=tile.position)
                row = existing.get(tile.id)
                if row is None:
                    session.add(
                        WidgetInstance(
                            id=tile.id,
                            workspace_id=ctx.workspace_uuid,
                            installation_id=tile.installation_id,
                            tile_id=tile.tile_id,
                            desktop_size=tile.size,
                            desktop_position=tile.position,
                            mobile_size=mobile.size,
                            mobile_position=mobile.position,
                            settings=tile.settings or {},
                        )
                    )
                    continue
                if row.installation_id != tile.installation_id or row.tile_id != tile.tile_id:
                    raise InvalidWorkspaceRequestError("A tile cannot change its plugin tile.")
                row.desktop_size = tile.size
                row.desktop_position = tile.position
                row.mobile_size = mobile.size
                row.mobile_position = mobile.position
                if tile.settings is not None and tile.settings != row.settings:
                    row.settings = tile.settings
                    row.state_version += 1
            await session.flush()
            view = await self._dashboard(session, ctx, new_version)
            return MutationResult(200, _dump(view))

        return await self._mutate(
            ctx,
            idempotency_key,
            request_hash("put-dashboard", "", payload.model_dump(mode="json")),
            work,
        )

    async def patch_tile(
        self,
        ctx: WorkspaceContext,
        tile_id: uuid.UUID,
        payload: PatchTileRequest,
        *,
        idempotency_key: str,
    ) -> MutationResult:
        async def work(session: AsyncSession) -> MutationResult:
            row = await session.scalar(
                update(WidgetInstance)
                .where(
                    WidgetInstance.id == tile_id,
                    WidgetInstance.workspace_id == ctx.workspace_uuid,
                    WidgetInstance.state_version == payload.expected_version,
                )
                .values(
                    settings=payload.settings,
                    state_version=WidgetInstance.state_version + 1,
                    updated_at=func.now(),
                )
                .returning(WidgetInstance)
            )
            if row is None:
                current = await session.scalar(
                    select(WidgetInstance.state_version).where(
                        WidgetInstance.id == tile_id,
                        WidgetInstance.workspace_id == ctx.workspace_uuid,
                    )
                )
                if current is None:
                    raise ResourceNotFoundError("The tile was not found.")
                raise VersionConflictError(current)
            return MutationResult(200, _dump(tile_view(row)))

        return await self._mutate(
            ctx,
            idempotency_key,
            request_hash("patch-tile", str(tile_id), payload.model_dump(mode="json")),
            work,
        )

    # ----------------------------------------------------------------- reviews

    async def upsert_review(self, user_id: str, plugin_id: str, rating: int, text: str) -> None:
        """Store API is M15; persistence lives here so the web import has a target."""
        if not 1 <= rating <= 5:
            raise InvalidWorkspaceRequestError("Rating must be between 1 and 5.")
        async with self._session_factory() as session, session.begin():
            await session.execute(
                insert(Review)
                .values(plugin_id=plugin_id, user_id=uuid.UUID(user_id), rating=rating, text=text)
                .on_conflict_do_update(
                    constraint="uq_plugin_reviews_plugin_user",
                    set_={"rating": rating, "text": text, "updated_at": func.now()},
                )
            )

    async def delete_review(self, user_id: str, plugin_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(Review).where(
                    Review.plugin_id == plugin_id, Review.user_id == uuid.UUID(user_id)
                )
            )

    async def list_reviews(self, plugin_id: str, *, limit: int = 100) -> list[Review]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Review)
                .where(Review.plugin_id == plugin_id)
                .order_by(Review.updated_at.desc())
                .limit(limit)
            )
            return list(rows)

    # ---------------------------------------------------------------- internals

    async def _mutate(
        self,
        ctx: WorkspaceContext,
        idempotency_key: str,
        fingerprint: str,
        work: Callable[[AsyncSession], Awaitable[MutationResult]],
    ) -> MutationResult:
        try:
            async with self._session_factory() as session, session.begin():
                replay = await _claim_idempotency_key(
                    session, ctx.workspace_uuid, idempotency_key, fingerprint
                )
                if replay is not None:
                    return replay
                result = await work(session)
                await session.execute(
                    update(WorkspaceMutation)
                    .where(
                        WorkspaceMutation.workspace_id == ctx.workspace_uuid,
                        WorkspaceMutation.idempotency_key == idempotency_key,
                    )
                    .values(status_code=result.status_code, response=result.body)
                )
                return result
        except IntegrityError as exc:
            # A concurrent request created the same unique row first.
            raise ConflictError("The resource was changed by another request.") from exc

    @staticmethod
    async def _workspace(session: AsyncSession, ctx: WorkspaceContext) -> Workspace:
        workspace = await session.scalar(
            select(Workspace).where(
                Workspace.id == ctx.workspace_uuid,
                Workspace.owner_user_id == uuid.UUID(ctx.user_id),
            )
        )
        if workspace is None:
            raise ResourceNotFoundError("The workspace was not found.")
        return workspace

    @staticmethod
    async def _installation(
        session: AsyncSession, ctx: WorkspaceContext, installation_id: uuid.UUID
    ) -> Installation:
        row = await session.scalar(
            select(Installation).where(
                Installation.id == installation_id,
                Installation.workspace_id == ctx.workspace_uuid,
            )
        )
        if row is None:
            raise ResourceNotFoundError("The installation was not found.")
        return row

    @staticmethod
    async def _dashboard(
        session: AsyncSession, ctx: WorkspaceContext, version: int
    ) -> DashboardView:
        rows = await session.scalars(
            select(WidgetInstance)
            .where(WidgetInstance.workspace_id == ctx.workspace_uuid)
            .order_by(WidgetInstance.desktop_position, WidgetInstance.id)
            .execution_options(populate_existing=True)
        )
        return DashboardView(expected_version=version, tiles=[tile_view(row) for row in rows])


async def ensure_personal_workspace(session: AsyncSession, owner: uuid.UUID) -> uuid.UUID:
    workspace_id = await session.scalar(
        select(Workspace.id).where(Workspace.owner_user_id == owner)
    )
    if workspace_id is not None:
        return workspace_id
    await session.execute(
        insert(Workspace)
        .values(id=uuid.uuid4(), owner_user_id=owner)
        .on_conflict_do_nothing(constraint="uq_workspaces_owner_user_id")
    )
    created = await session.scalar(select(Workspace.id).where(Workspace.owner_user_id == owner))
    if created is None:
        raise ResourceNotFoundError("The workspace was not found.")
    return created


async def compare_and_set_plugin_state(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    plugin_id: str,
    data: dict[str, Any],
    expected_version: int,
) -> int:
    """CAS plugin data inside the caller's transaction; returns the new version.

    Builtin plugins (M14) call this directly with their own session.
    """
    if expected_version == 0:
        created = await session.scalar(
            insert(PluginState)
            .values(workspace_id=workspace_id, plugin_id=plugin_id, data=data, state_version=1)
            .on_conflict_do_nothing()
            .returning(PluginState.state_version)
        )
        if created is not None:
            return int(created)
    else:
        updated = await session.scalar(
            update(PluginState)
            .where(
                PluginState.workspace_id == workspace_id,
                PluginState.plugin_id == plugin_id,
                PluginState.state_version == expected_version,
            )
            .values(data=data, state_version=PluginState.state_version + 1, updated_at=func.now())
            .returning(PluginState.state_version)
        )
        if updated is not None:
            return int(updated)
    current = await session.scalar(
        select(PluginState.state_version).where(
            PluginState.workspace_id == workspace_id, PluginState.plugin_id == plugin_id
        )
    )
    raise VersionConflictError(int(current or 0))


async def _claim_idempotency_key(
    session: AsyncSession, workspace_id: uuid.UUID, key: str, fingerprint: str
) -> MutationResult | None:
    await session.execute(
        delete(WorkspaceMutation).where(
            WorkspaceMutation.workspace_id == workspace_id,
            WorkspaceMutation.created_at < datetime.now(UTC) - IDEMPOTENCY_RETENTION,
        )
    )
    # A concurrent holder of the same key blocks this insert until it commits or
    # rolls back, so the stored response is complete whenever it is visible.
    claimed = await session.scalar(
        insert(WorkspaceMutation)
        .values(workspace_id=workspace_id, idempotency_key=key, request_hash=fingerprint)
        .on_conflict_do_nothing()
        .returning(WorkspaceMutation.idempotency_key)
    )
    if claimed is not None:
        return None
    stored = await session.scalar(
        select(WorkspaceMutation)
        .where(
            WorkspaceMutation.workspace_id == workspace_id,
            WorkspaceMutation.idempotency_key == key,
        )
        .execution_options(populate_existing=True)
    )
    if stored is None or stored.request_hash != fingerprint or stored.status_code is None:
        raise IdempotencyConflictError("The Idempotency-Key was used for another request.")
    return MutationResult(stored.status_code, stored.response)


def _validated_custom_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise InvalidWorkspaceRequestError("The MCP server URL must be a plain HTTPS URL.")
    return url


def _dump(model: Any) -> dict[str, Any]:
    return dict(model.model_dump(mode="json"))
