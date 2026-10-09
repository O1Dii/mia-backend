"""The store: publish releases (admin), search and read the catalog, reviews.

A release is immutable. Publishing runs, in order: admin check, schema and file checks,
``server/discover`` + ``tools/list`` (same client and SSRF guard as the router), overlay =
``tools/list`` check, UI markup check, then stores manifest, consented tool snapshot, the
router-compatible ``tools_digest`` and the package files.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.modules.plugins.catalog.discovery import (
    Discovered,
    DiscoveryError,
    discover,
    token_connector,
)
from app.modules.plugins.catalog.errors import (
    PackageInvalidError,
    PublishForbiddenError,
    ReleaseExistsError,
    StoreUnavailableError,
)
from app.modules.plugins.catalog.models import PluginRelease
from app.modules.plugins.catalog.package import (
    IMAGE_SUFFIXES,
    PackageFile,
    PluginPackage,
    decode_files,
    encode_files,
)
from app.modules.plugins.catalog.schemas import (
    CatalogEntryDetail,
    CatalogEntryView,
    CatalogPage,
    ConsentView,
    RatingView,
    ReviewList,
    ReviewView,
)
from app.modules.plugins.catalog.validation import (
    ContractsUnavailableError,
    Problem,
    package_problems,
)
from app.modules.plugins.catalog.versions import version_key
from app.modules.plugins.install.consent import build_consent, requires_connection
from app.modules.plugins.install.grants import required_grants
from app.modules.tools.mcp_client import InProcessServerFactory, McpConnector
from app.modules.users.models import User
from app.modules.workspace.catalog import CatalogPlugin
from app.modules.workspace.errors import InvalidWorkspaceRequestError, ResourceNotFoundError
from app.modules.workspace.models import Installation, Review

MAX_PAGE = 100


@dataclass(frozen=True, slots=True)
class PublishResult:
    release: PluginRelease
    created: bool


def _problem_dicts(problems: Iterable[Problem]) -> list[dict[str, str]]:
    return [{"code": p.code, "path": p.path, "message": p.message} for p in problems]


def tools_snapshot(package: PluginPackage, discovered: Discovered) -> list[dict[str, Any]]:
    """Consented tools: the server's listing joined with the manifest's risk overlay."""
    policies = package.manifest["tools"]
    snapshot: list[dict[str, Any]] = []
    for tool in sorted(discovered.tools, key=lambda item: item.name):
        policy = policies.get(tool.name)
        if policy is None:  # app-only get_state of declarative servers
            continue
        snapshot.append(
            {
                "name": tool.name,
                "description": tool.description,
                "risk": policy["risk"],
                "voice": policy.get("voice") is True,
            }
        )
    return snapshot


class CatalogService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        server_factory: InProcessServerFactory | None = None,
        admin_user_ids: frozenset[str] = frozenset(),
        connector_factory: Callable[[str | None], McpConnector] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._server_factory = server_factory
        self._admins = admin_user_ids
        self._connector_factory = connector_factory or (
            lambda token: token_connector(token, server_factory=server_factory)
        )

    # ------------------------------------------------------------------ publish

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins

    async def publish(
        self,
        package: PluginPackage,
        *,
        actor_user_id: str,
        store_meta: dict[str, Any] | None = None,
        discover_token: str | None = None,
    ) -> PublishResult:
        if not self.is_admin(actor_user_id):
            raise PublishForbiddenError("Only a store administrator can publish plugins.")
        try:
            problems = package_problems(package)
        except ContractsUnavailableError as error:
            raise StoreUnavailableError("The plugin contract is not available.") from error
        if problems:
            raise PackageInvalidError(_problem_dicts(problems))
        manifest = package.manifest
        if manifest["server"]["kind"] == "builtin" and package.directory is None:
            raise PackageInvalidError(
                _problem_dicts(
                    [Problem("package_file_missing", "server", "builtin needs a directory")]
                )
            )
        try:
            discovered = await discover(
                package,
                connector=self._connector_factory(discover_token),
                server_factory=self._server_factory,
            )
        except DiscoveryError as error:
            raise PackageInvalidError(_problem_dicts(error.problems)) from error
        listing = manifest["listing"]
        row = PluginRelease(
            plugin_id=manifest["id"],
            version=manifest["version"],
            version_key=version_key(manifest["version"]),
            status="published",
            name=manifest["name"],
            description=listing["description"],
            category=listing["category"],
            author=listing.get("author"),
            server_kind=manifest["server"]["kind"],
            manifest=manifest,
            tools=tools_snapshot(package, discovered),
            tools_digest=discovered.digest,
            grants=sorted(required_grants(manifest)),
            store_meta=store_meta or {},
            files=encode_files(package.files),
            package_digest=package.digest(),
            published_by=uuid.UUID(actor_user_id),
        )
        return await self._insert_release(row)

    async def _insert_release(self, row: PluginRelease) -> PublishResult:
        async with self._session_factory() as session:
            existing = await self._get_release(session, row.plugin_id, row.version)
            if existing is None:
                session.add(row)
                try:
                    await session.commit()
                except IntegrityError:  # a concurrent publisher won the race
                    await session.rollback()
                    existing = await self._get_release(session, row.plugin_id, row.version)
                else:
                    await session.refresh(row)
                    session.expunge(row)
                    return PublishResult(row, created=True)
            assert existing is not None
            if (
                existing.package_digest == row.package_digest
                and existing.tools_digest == row.tools_digest
            ):
                session.expunge(existing)
                return PublishResult(existing, created=False)
            raise ReleaseExistsError(
                f"{row.plugin_id} {row.version} is already published with different content; "
                "publish a new version."
            )

    async def yank(self, plugin_id: str, version: str, *, actor_user_id: str) -> None:
        """Hide a release from the catalog and from new installs (installed copies keep working)."""
        if not self.is_admin(actor_user_id):
            raise PublishForbiddenError("Only a store administrator can yank releases.")
        async with self._session_factory() as session, session.begin():
            row = await self._get_release(session, plugin_id, version)
            if row is None:
                raise ResourceNotFoundError("The release was not found.")
            row.status = "yanked"

    # --------------------------------------------------------------------- read

    @staticmethod
    async def _get_release(
        session: AsyncSession, plugin_id: str, version: str
    ) -> PluginRelease | None:
        return cast(
            PluginRelease | None,
            await session.scalar(
                select(PluginRelease).where(
                    PluginRelease.plugin_id == plugin_id, PluginRelease.version == version
                )
            ),
        )

    async def release(self, plugin_id: str, version: str | None = None) -> PluginRelease:
        """A published release (the latest when ``version`` is omitted); 404 otherwise."""
        async with self._session_factory() as session:
            query = select(PluginRelease).where(
                PluginRelease.plugin_id == plugin_id, PluginRelease.status == "published"
            )
            if version is not None:
                query = query.where(PluginRelease.version == version)
            row = await session.scalar(query.order_by(PluginRelease.version_key.desc()).limit(1))
            if row is None:
                raise ResourceNotFoundError("The plugin was not found.")
            session.expunge(row)
            return row

    async def any_release(self, plugin_id: str, version: str) -> PluginRelease | None:
        """Any release including yanked ones (installed copies of a yanked version)."""
        async with self._session_factory() as session:
            row = await self._get_release(session, plugin_id, version)
            if row is not None:
                session.expunge(row)
            return row

    async def versions(self, plugin_id: str) -> list[str]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(PluginRelease.version)
                .where(PluginRelease.plugin_id == plugin_id, PluginRelease.status == "published")
                .order_by(PluginRelease.version_key.desc())
            )
            return [version for (version,) in rows]

    async def resolve(self, plugin_id: str, version: str) -> CatalogPlugin | None:
        """``PluginCatalog`` port of M10: only published releases can be installed."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(PluginRelease).where(
                    PluginRelease.plugin_id == plugin_id,
                    PluginRelease.version == version,
                    PluginRelease.status == "published",
                )
            )
            if row is None:
                return None
            return CatalogPlugin(
                plugin_id=row.plugin_id,
                version=row.version,
                tools_digest=row.tools_digest,
                grants=tuple(sorted(row.grants)),
                manifest=dict(row.manifest),
                requires_connection=requires_connection(row.manifest),
            )

    async def list_entries(
        self, *, query: str | None, category: str | None, limit: int, cursor: str | None
    ) -> CatalogPage:
        limit = max(1, min(limit, MAX_PAGE))
        latest = (
            select(PluginRelease)
            .where(PluginRelease.status == "published")
            .distinct(PluginRelease.plugin_id)
            .order_by(PluginRelease.plugin_id, PluginRelease.version_key.desc())
            .subquery()
        )
        entry = aliased(PluginRelease, latest)
        statement = select(entry)
        if category:
            statement = statement.where(entry.category == category)
        if query:
            pattern = (
                "%"
                + query.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                + "%"
            )
            statement = statement.where(
                entry.name.ilike(pattern, escape="\\")
                | entry.description.ilike(pattern, escape="\\")
                | entry.plugin_id.ilike(pattern, escape="\\")
            )
        if cursor:
            name, plugin_id = _decode_cursor(cursor)
            statement = statement.where(tuple_(entry.name, entry.plugin_id) > (name, plugin_id))
        statement = statement.order_by(entry.name, entry.plugin_id).limit(limit + 1)
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
            page = rows[:limit]
            ratings = await _ratings(session, _alias_ids(page))
            installs = await _install_counts(session, [row.plugin_id for row in page])
            views = [
                _entry_view(row, ratings.get(row.plugin_id), installs.get(row.plugin_id, 0))
                for row in page
            ]
        next_cursor = (
            _encode_cursor(page[-1].name, page[-1].plugin_id) if len(rows) > limit else None
        )
        return CatalogPage(items=views, next_cursor=next_cursor)

    async def entry(self, plugin_id: str, version: str | None = None) -> CatalogEntryDetail:
        release = await self.release(plugin_id, version)
        async with self._session_factory() as session:
            ratings = await _ratings(session, _alias_ids([release]))
            installs = await _install_counts(session, [plugin_id])
        view = _entry_view(release, ratings.get(plugin_id), installs.get(plugin_id, 0))
        return CatalogEntryDetail(
            **view.model_dump(),
            versions=await self.versions(plugin_id),
            consent=consent_of(release),
        )

    async def file(self, plugin_id: str, version: str, path: str) -> PackageFile | None:
        """A listing image of a release (the only package files served over HTTP)."""
        row = await self.any_release(plugin_id, version)
        if row is None or not path.lower().endswith(tuple(IMAGE_SUFFIXES)):
            return None
        return decode_files(row.files).get(path)

    # ------------------------------------------------------------------ reviews

    async def reviews(self, user_id: str, plugin_id: str, *, limit: int = 50) -> ReviewList:
        release = await self.release(plugin_id)
        ids = _alias_ids([release])
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Review, User.display_name)
                    .join(User, User.id == Review.user_id)
                    .where(Review.plugin_id.in_(list(ids)))
                    .order_by(Review.updated_at.desc())
                    .limit(max(1, min(limit, MAX_PAGE)))
                )
            ).all()
            rating = (await _ratings(session, ids)).get(
                plugin_id, RatingView(average=None, count=0)
            )
        owner = uuid.UUID(user_id)
        return ReviewList(
            items=[
                ReviewView(
                    id=review.id,
                    plugin_id=plugin_id,
                    rating=review.rating,
                    text=review.text,
                    author=_first_name(name),
                    mine=review.user_id == owner,
                    updated_at=review.updated_at,
                )
                for review, name in rows
            ],
            rating=rating,
        )


def consent_of(release: PluginRelease) -> ConsentView:
    return build_consent(release.manifest, list(release.tools), release.tools_digest)


def _first_name(display_name: str | None) -> str:
    parts = (display_name or "").split()
    return parts[0][:40] if parts else "Пользователь"


def _alias_ids(releases: Iterable[PluginRelease]) -> dict[str, str]:
    """``{stored review plugin_id: shown plugin_id}``: the plugin itself and its web POC id."""
    mapping: dict[str, str] = {}
    for release in releases:
        mapping[release.plugin_id] = release.plugin_id
        legacy = release.store_meta.get("legacy_id")
        if isinstance(legacy, str):
            mapping.setdefault(legacy, release.plugin_id)
    return mapping


async def _ratings(session: AsyncSession, ids: dict[str, str]) -> dict[str, RatingView]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Review.plugin_id, func.count(), func.avg(Review.rating))
        .where(Review.plugin_id.in_(list(ids)))
        .group_by(Review.plugin_id)
    )
    totals: dict[str, tuple[int, float]] = {}
    for stored, count, average in rows:
        shown = ids[stored]
        previous = totals.get(shown, (0, 0.0))
        totals[shown] = (previous[0] + count, previous[1] + float(average) * count)
    return {
        shown: RatingView(average=round(total / count, 2), count=count)
        for shown, (count, total) in totals.items()
    }


async def _install_counts(session: AsyncSession, plugin_ids: list[str]) -> dict[str, int]:
    if not plugin_ids:
        return {}
    rows = await session.execute(
        select(Installation.plugin_id, func.count())
        .where(Installation.plugin_id.in_(plugin_ids), Installation.source == "catalog")
        .group_by(Installation.plugin_id)
    )
    return {plugin_id: count for plugin_id, count in rows}


def _entry_view(
    release: PluginRelease, rating: RatingView | None, installs: int
) -> CatalogEntryView:
    listing = release.manifest.get("listing", {})
    base = f"/v1/plugins/{release.plugin_id}/releases/{release.version}/files/"
    icon = listing.get("icon")
    meta = release.store_meta
    return CatalogEntryView(
        plugin_id=release.plugin_id,
        version=release.version,
        manifest=dict(release.manifest),
        name=release.name,
        description=release.description,
        category=release.category,
        author=release.author,
        kind=cast(Literal["builtin", "remote", "declarative"], release.server_kind),
        tagline=_str(meta.get("tagline")),
        color=_str(meta.get("color")),
        art=_str(meta.get("art")),
        featured=meta.get("featured") is True,
        rating=rating or RatingView(average=None, count=0),
        installs=installs,
        icon_url=base + icon if isinstance(icon, str) else None,
        screenshots=[base + path for path in listing.get("screenshots", [])],
        published_at=release.published_at,
    )


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _encode_cursor(name: str, plugin_id: str) -> str:
    raw = json.dumps([name, plugin_id], ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        name, plugin_id = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(name, str) or not isinstance(plugin_id, str):
            raise ValueError
    except (binascii.Error, ValueError, TypeError) as error:
        raise InvalidWorkspaceRequestError("The cursor is not valid.") from error
    return name, plugin_id
