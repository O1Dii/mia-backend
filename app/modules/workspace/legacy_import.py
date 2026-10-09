"""Import web-POC SQLite accounts (`app/web_poc/accounts.py`) into Postgres.

Rules (Gap Analysis §6.2, OI-WEB-005):

- The owner is matched by the verified Google ``sub`` only, never by email.
  Web-POC accounts created through the shared hub carry ``hub:<uuid>`` instead of
  a Google ``sub``; they are imported only when the operator supplies an explicit
  identity map ``{"hub:<uuid>": "<google sub>"}``; otherwise they are skipped.
- A legacy account already imported into a different MIA user is refused
  (``owner_mismatch``), so changing the identity map cannot move data.
- Everything is insert-if-absent with deterministic keys: re-running is a no-op
  and never overwrites data created in Postgres after the first import.
- Imported: installations (``source = legacy_web``), plugin states, layout
  tiles, reviews. Not imported: cookie sessions (users sign in again), sign-in
  challenges, the in-memory BYOK connections (never stored in SQLite), the
  action audit trail, temporary SDK grants, anonymous local workspaces.
- The SQLite file is opened read-only. Reports carry ids and counts only, no
  emails, names or plugin data.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.users.models import User
from app.modules.workspace.models import (
    Installation,
    LegacyWebImport,
    PluginState,
    Review,
    WidgetInstance,
    Workspace,
)
from app.modules.workspace.service import ensure_personal_workspace

LEGACY_NAMESPACE = uuid.UUID("6f1d2c1e-3a4b-5c6d-8e9f-a0b1c2d3e4f5")
REQUIRED_COLUMNS = {
    "web_users": {"id", "google_sub", "name", "email"},
    "web_workspaces": {"user_id", "document"},
    "web_reviews": {"plugin_id", "user_id", "rating", "text", "updated"},
}
LEGACY_SIZES = {"small": "1x1", "medium": "2x1", "wide": "4x2"}
PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
LEGACY_TILE_ID = "main"


class LegacySchemaError(ValueError):
    """The SQLite file is not a web-POC account database this importer understands."""


@dataclass(frozen=True, slots=True)
class LegacyAccount:
    legacy_user_id: str
    legacy_subject: str
    name: str
    email: str
    document: dict[str, Any] | None
    document_error: bool
    reviews: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class LegacySnapshot:
    accounts: tuple[LegacyAccount, ...]
    orphan_rows: int


@dataclass(frozen=True, slots=True)
class ImportOptions:
    source_id: str = "web_poc"
    owner_subs: frozenset[str] | None = None
    """Import only these verified Google subs (``--owner-sub``)."""
    identity_map: Mapping[str, str] = field(default_factory=dict)
    """Legacy subject (``hub:<uuid>``) → verified Google ``sub``."""
    dry_run: bool = False


@dataclass(slots=True)
class AccountReport:
    legacy_user_id: str
    status: str
    """``imported`` | ``unchanged`` | ``skipped``."""
    reason: str | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    created_user: bool = False
    email_differs: bool = False
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ImportReport:
    source_id: str
    dry_run: bool
    orphan_rows: int
    accounts: list[AccountReport]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "dry_run": self.dry_run,
            "orphan_rows": self.orphan_rows,
            "accounts": [asdict(account) for account in self.accounts],
        }


def read_legacy_database(path: Path) -> LegacySnapshot:
    """Read the web-POC SQLite file without modifying it."""
    if not path.is_file():
        raise LegacySchemaError("SQLite file not found")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        for table, columns in REQUIRED_COLUMNS.items():
            present = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not columns <= present:
                raise LegacySchemaError(f"table {table} is missing or has an unknown schema")
        users = connection.execute("SELECT id, google_sub, name, email FROM web_users").fetchall()
        user_ids = {str(row["id"]) for row in users}
        documents = {
            str(row["user_id"]): str(row["document"])
            for row in connection.execute("SELECT user_id, document FROM web_workspaces")
        }
        reviews: dict[str, list[dict[str, Any]]] = {}
        for row in connection.execute(
            "SELECT plugin_id, user_id, rating, text, updated FROM web_reviews"
        ):
            reviews.setdefault(str(row["user_id"]), []).append(dict(row))
    except sqlite3.DatabaseError as exc:
        raise LegacySchemaError("SQLite file cannot be read") from exc
    finally:
        connection.close()
    orphan_rows = sum(1 for owner in documents if owner not in user_ids) + sum(
        len(items) for owner, items in reviews.items() if owner not in user_ids
    )
    accounts = []
    for row in users:
        legacy_id = str(row["id"])
        raw = documents.get(legacy_id)
        document: dict[str, Any] | None = None
        document_error = False
        if raw is not None:
            try:
                parsed = json.loads(raw)
                if (
                    not isinstance(parsed, dict)
                    or not isinstance(parsed.get("installed", {}), dict)
                    or not isinstance(parsed.get("states", {}), dict)
                    or not isinstance(parsed.get("layout", []), list)
                ):
                    raise ValueError("unexpected workspace document")
                document = parsed
            except ValueError:
                document_error = True
        accounts.append(
            LegacyAccount(
                legacy_user_id=legacy_id,
                legacy_subject=str(row["google_sub"]),
                name=str(row["name"]),
                email=str(row["email"]),
                document=document,
                document_error=document_error,
                reviews=tuple(reviews.get(legacy_id, ())),
            )
        )
    return LegacySnapshot(accounts=tuple(accounts), orphan_rows=orphan_rows)


async def import_legacy_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: LegacySnapshot,
    options: ImportOptions,
) -> ImportReport:
    async with session_factory() as session:
        ready = await session.scalar(text("SELECT to_regclass('public.legacy_web_imports')"))
    if ready is None:
        raise LegacySchemaError("target database is not migrated (run alembic upgrade head)")
    report = ImportReport(
        source_id=options.source_id,
        dry_run=options.dry_run,
        orphan_rows=snapshot.orphan_rows,
        accounts=[],
    )
    for account in snapshot.accounts:
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                result = await _import_account(session, account, options)
            except BaseException:
                await transaction.rollback()
                raise
            if options.dry_run:
                await transaction.rollback()
            else:
                await transaction.commit()
        report.accounts.append(result)
    return report


def _verified_subject(account: LegacyAccount, options: ImportOptions) -> str | None:
    if account.legacy_subject.startswith("hub:"):
        mapped = options.identity_map.get(account.legacy_subject)
        return mapped if mapped and not mapped.startswith("hub:") else None
    return account.legacy_subject or None


def _digest(account: LegacyAccount) -> str:
    canonical = json.dumps(
        {"document": account.document, "reviews": account.reviews},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _import_account(
    session: AsyncSession, account: LegacyAccount, options: ImportOptions
) -> AccountReport:
    report = AccountReport(legacy_user_id=account.legacy_user_id, status="skipped")
    subject = _verified_subject(account, options)
    if subject is None:
        report.reason = "unmapped_hub_identity"
        return report
    if options.owner_subs is not None and subject not in options.owner_subs:
        report.reason = "not_selected_owner"
        return report
    if account.document_error:
        report.reason = "invalid_document"
        return report

    user = await session.scalar(select(User).where(User.google_sub == subject))
    if user is not None and user.deleted_at is not None:
        report.reason = "deleted_account"
        return report
    previous = await session.get(LegacyWebImport, (options.source_id, account.legacy_user_id))
    if previous is not None and (user is None or previous.user_id != user.id):
        report.reason = "owner_mismatch"
        return report
    if user is None:
        user = User(
            google_sub=subject,
            email=account.email[:320],
            email_verified=not account.legacy_subject.startswith("hub:"),
            display_name=account.name[:160] or None,
        )
        session.add(user)
        await session.flush()
        report.created_user = True
    else:
        report.email_differs = user.email.casefold() != account.email.casefold()

    workspace_id = await ensure_personal_workspace(session, user.id)
    report.user_id = str(user.id)
    report.workspace_id = str(workspace_id)
    digest = _digest(account)
    counts = await _import_document(session, workspace_id, account.document or {})
    counts["reviews"] = await _import_reviews(session, user.id, account.reviews)
    report.counts = counts
    written = sum(value for key, value in counts.items() if key != "skipped_items")
    report.status = "unchanged" if previous is not None and not written else "imported"
    await session.execute(
        insert(LegacyWebImport)
        .values(
            source_id=options.source_id,
            legacy_user_id=account.legacy_user_id,
            user_id=user.id,
            workspace_id=workspace_id,
            document_digest=digest,
            counts=counts,
        )
        .on_conflict_do_update(
            index_elements=[LegacyWebImport.source_id, LegacyWebImport.legacy_user_id],
            set_={"document_digest": digest, "imported_at": datetime.now(UTC)},
        )
    )
    return report


async def _import_document(
    session: AsyncSession, workspace_id: uuid.UUID, document: dict[str, Any]
) -> dict[str, int]:
    counts = {"installations": 0, "plugin_states": 0, "tiles": 0, "skipped_items": 0}
    installed: dict[str, Any] = document.get("installed", {})
    for plugin_id, manifest in installed.items():
        if not PLUGIN_ID.match(plugin_id) or not isinstance(manifest, dict):
            counts["skipped_items"] += 1
            continue
        version = manifest.get("version")
        permissions = manifest.get("permissions", [])
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        created = await session.scalar(
            insert(Installation)
            .values(
                id=uuid.uuid5(LEGACY_NAMESPACE, f"{workspace_id}:installation:{plugin_id}"),
                workspace_id=workspace_id,
                plugin_id=plugin_id,
                version=version if isinstance(version, str) and VERSION.match(version) else "0.0.0",
                status="active",
                source="legacy_web",
                grants=sorted({item for item in permissions if isinstance(item, str)})
                if isinstance(permissions, list)
                else [],
                tools_digest="legacy:sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
                manifest=manifest,
            )
            .on_conflict_do_nothing()
            .returning(Installation.id)
        )
        counts["installations"] += created is not None

    states: dict[str, Any] = document.get("states", {})
    for plugin_id, data in states.items():
        if not PLUGIN_ID.match(plugin_id) or not isinstance(data, dict):
            counts["skipped_items"] += 1
            continue
        created = await session.scalar(
            insert(PluginState)
            .values(workspace_id=workspace_id, plugin_id=plugin_id, data=data, state_version=1)
            .on_conflict_do_nothing()
            .returning(PluginState.plugin_id)
        )
        counts["plugin_states"] += created is not None

    owned = {
        plugin_id: installation_id
        for plugin_id, installation_id in await session.execute(
            select(Installation.plugin_id, Installation.id).where(
                Installation.workspace_id == workspace_id
            )
        )
    }
    layout: list[Any] = document.get("layout", [])
    for position, widget in enumerate(layout):
        if not isinstance(widget, dict) or widget.get("plugin_id") not in owned:
            counts["skipped_items"] += 1
            continue
        size = LEGACY_SIZES.get(str(widget.get("size")), "2x1")
        created = await session.scalar(
            insert(WidgetInstance)
            .values(
                id=uuid.uuid5(LEGACY_NAMESPACE, f"{workspace_id}:tile:{widget.get('id')}"),
                workspace_id=workspace_id,
                installation_id=owned[widget["plugin_id"]],
                tile_id=LEGACY_TILE_ID,
                desktop_size=size,
                desktop_position=position,
                mobile_size=size,
                mobile_position=position,
            )
            .on_conflict_do_nothing()
            .returning(WidgetInstance.id)
        )
        counts["tiles"] += created is not None
    if counts["tiles"]:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(layout_version=Workspace.layout_version + 1)
        )
    return counts


async def _import_reviews(
    session: AsyncSession, user_id: uuid.UUID, reviews: tuple[dict[str, Any], ...]
) -> int:
    imported = 0
    for review in reviews:
        plugin_id = str(review["plugin_id"])
        rating = review["rating"]
        if not PLUGIN_ID.match(plugin_id) or not isinstance(rating, int) or not 1 <= rating <= 5:
            continue
        updated = datetime.fromtimestamp(float(review["updated"]), UTC)
        created = await session.scalar(
            insert(Review)
            .values(
                plugin_id=plugin_id,
                user_id=user_id,
                rating=rating,
                text=str(review["text"])[:2000],
                created_at=updated,
                updated_at=updated,
            )
            .on_conflict_do_nothing(constraint="uq_plugin_reviews_plugin_user")
            .returning(Review.id)
        )
        imported += created is not None
    return imported
