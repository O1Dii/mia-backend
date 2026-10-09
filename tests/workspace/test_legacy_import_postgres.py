"""Web-POC SQLite → Postgres import: owner check, idempotency, read-only source."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.modules.auth.google import GoogleIdentity
from app.modules.users.models import User
from app.modules.workspace.legacy_import import (
    ImportOptions,
    LegacySchemaError,
    import_legacy_snapshot,
    read_legacy_database,
)
from app.modules.workspace.models import (
    Installation,
    PluginState,
    Review,
    WidgetInstance,
    Workspace,
)
from sqlalchemy import func, select

from tests.workspace.harness import (
    Harness,
    android_login,
    database_url,
    requires_postgres,
)

pytestmark = requires_postgres
REPO_ROOT = Path(__file__).resolve().parents[2]


def identity(subject: str, email: str) -> GoogleIdentity:
    return GoogleIdentity(subject, email, True, subject.title(), None, "ru")


LEGACY_SCHEMA = """
CREATE TABLE web_users (
  id TEXT PRIMARY KEY, google_sub TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL, email TEXT NOT NULL);
CREATE TABLE web_sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES web_users(id),
  expires REAL NOT NULL);
CREATE TABLE web_challenges (
  cookie_hash TEXT PRIMARY KEY, nonce TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE web_workspaces (
  user_id TEXT PRIMARY KEY REFERENCES web_users(id), document TEXT NOT NULL);
CREATE TABLE web_reviews (
  plugin_id TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES web_users(id),
  rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
  text TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(plugin_id,user_id));
"""
"""The web-POC SQLite schema, frozen here when ``app/web_poc`` was removed (M31)."""


def _widget(plugin_id: str) -> dict[str, str]:
    return {"id": plugin_id, "plugin_id": plugin_id, "size": "medium"}


def build_legacy_database(path: Path) -> dict[str, str]:
    """Create a web-POC database in the POC's own layout; return legacy user ids."""
    ids = {"alice": str(uuid.uuid4()), "bob": str(uuid.uuid4()), "hub": str(uuid.uuid4())}
    db = sqlite3.connect(path)
    try:
        db.executescript(LEGACY_SCHEMA)
        for key, subject, email in (
            ("alice", "google-alice", "alice@example.test"),
            ("bob", "google-bob", "bob@example.test"),
            ("hub", "hub:7f6c1d3e-2b1a-4c5d-9e8f-0a1b2c3d4e5f", "hub@ex.test"),
        ):
            db.execute(
                "INSERT INTO web_users VALUES (?,?,?,?)",
                (ids[key], subject, subject.split(":")[0].title(), email),
            )

        def manifest(plugin_id: str) -> dict[str, str]:
            return {"id": plugin_id, "schemaVersion": "2.0", "name": plugin_id}

        alice_document = {
            "installed": {name: manifest(name) for name in ("mia.timer", "mia.notes")},
            "states": {"mia.timer": {"left": 42}, "mia.notes": {"items": ["купить хлеб"]}},
            "layout": [_widget("mia.timer"), _widget("mia.notes")],
            "revision": 2,
            "audit": [{"action": "secret-ish audit"}],
        }
        hub_document = {
            "installed": {"mia.timer": manifest("mia.timer")},
            "states": {"mia.timer": {}},
            "layout": [_widget("mia.timer")],
            "revision": 1,
            "audit": [],
        }
        for key, document in (("alice", alice_document), ("hub", hub_document)):
            db.execute(
                "INSERT INTO web_workspaces VALUES (?,?)",
                (ids[key], json.dumps(document, ensure_ascii=False)),
            )
        for user, rating, text in (("alice", 5, "Отличный таймер"), ("bob", 3, "Нормально")):
            db.execute(
                "INSERT INTO web_reviews VALUES (?,?,?,?,?)",
                ("mia.timer", ids[user], rating, text, time.time()),
            )
        db.commit()
    finally:
        db.close()
    return ids


def copy_of(source: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "copy.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        if Path(f"{source}{suffix}").exists():
            shutil.copy(f"{source}{suffix}", f"{copy}{suffix}")
    return copy


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def by_legacy_id(report: Any) -> dict[str, Any]:
    return {account.legacy_user_id: account for account in report.accounts}


async def counts(harness: Harness) -> dict[str, int]:
    async with harness.factory() as session:
        return {
            model.__tablename__: int(
                await session.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in (User, Workspace, Installation, PluginState, WidgetInstance, Review)
        }


async def test_import_links_by_sub_is_idempotent_and_never_touches_source(
    harness: Harness, tmp_path: Path
) -> None:
    original = tmp_path / "web.sqlite3"
    ids = build_legacy_database(original)
    copy = copy_of(original, tmp_path)
    before = file_digest(copy)

    # Alice already uses Android: her data must land in that same account.
    async with harness.client() as client:
        bearer = await android_login(client, "google-alice", "alice-phone@example.test")
        alice_user_id = (await client.get("/v1/me", headers=bearer)).json()["id"]

    snapshot = read_legacy_database(copy)
    assert snapshot.orphan_rows == 0
    report = await import_legacy_snapshot(harness.factory, snapshot, ImportOptions())
    accounts = by_legacy_id(report)
    alice = accounts[ids["alice"]]
    assert alice.status == "imported" and alice.user_id == alice_user_id
    assert alice.created_user is False and alice.email_differs is True
    assert alice.counts == {
        "installations": 2,
        "plugin_states": 2,
        "tiles": 2,
        "skipped_items": 0,
        "reviews": 1,
    }
    assert accounts[ids["bob"]].status == "imported" and accounts[ids["bob"]].created_user
    hub = accounts[ids["hub"]]
    assert hub.status == "skipped" and hub.reason == "unmapped_hub_identity"
    assert file_digest(copy) == before
    serialized = json.dumps(report.as_dict(), ensure_ascii=False)
    for private in ("alice@example.test", "купить хлеб", "Отличный", "secret-ish"):
        assert private not in serialized

    async with harness.client() as client:
        dashboard = (await client.get("/v1/dashboard", headers=bearer)).json()
        installations = (await client.get("/v1/installations", headers=bearer)).json()["items"]
    assert dashboard["expected_version"] == 1
    assert [item["size"] for item in dashboard["tiles"]] == ["2x1", "2x1"]
    assert {item["plugin_id"] for item in installations} == {"mia.timer", "mia.notes"}
    assert all(item["source"] == "legacy_web" for item in installations)
    async with harness.factory() as session:
        state = await session.scalar(
            select(PluginState.data).where(PluginState.plugin_id == "mia.notes")
        )
    assert state == {"items": ["купить хлеб"]}

    totals = await counts(harness)
    rerun = await import_legacy_snapshot(
        harness.factory, read_legacy_database(copy), ImportOptions()
    )
    assert by_legacy_id(rerun)[ids["alice"]].status == "unchanged"
    assert by_legacy_id(rerun)[ids["bob"]].status == "unchanged"
    assert await counts(harness) == totals
    async with harness.client() as client:
        assert (await client.get("/v1/dashboard", headers=bearer)).json()["expected_version"] == 1


async def test_owner_selection_identity_map_and_owner_mismatch(
    harness: Harness, tmp_path: Path
) -> None:
    ids = build_legacy_database(tmp_path / "web.sqlite3")
    snapshot = read_legacy_database(tmp_path / "web.sqlite3")

    only_bob = await import_legacy_snapshot(
        harness.factory, snapshot, ImportOptions(owner_subs=frozenset({"google-bob"}))
    )
    statuses = {key: value.status for key, value in by_legacy_id(only_bob).items()}
    assert statuses == {ids["alice"]: "skipped", ids["bob"]: "imported", ids["hub"]: "skipped"}
    assert by_legacy_id(only_bob)[ids["alice"]].reason == "not_selected_owner"

    hub_subject = "hub:7f6c1d3e-2b1a-4c5d-9e8f-0a1b2c3d4e5f"
    mapped = await import_legacy_snapshot(
        harness.factory, snapshot, ImportOptions(identity_map={hub_subject: "google-hub-real"})
    )
    hub = by_legacy_id(mapped)[ids["hub"]]
    assert hub.status == "imported" and hub.counts["installations"] == 1
    async with harness.factory() as session:
        created = await session.scalar(select(User).where(User.id == hub.user_id))
    assert created is not None and created.google_sub == "google-hub-real"
    assert created.email_verified is False

    moved = await import_legacy_snapshot(
        harness.factory, snapshot, ImportOptions(identity_map={hub_subject: "google-other"})
    )
    assert by_legacy_id(moved)[ids["hub"]].reason == "owner_mismatch"
    async with harness.factory() as session:
        assert await session.scalar(select(User).where(User.google_sub == "google-other")) is None


async def test_dry_run_writes_nothing_and_invalid_sources_are_rejected(
    harness: Harness, tmp_path: Path
) -> None:
    build_legacy_database(tmp_path / "web.sqlite3")
    before = await counts(harness)
    report = await import_legacy_snapshot(
        harness.factory, read_legacy_database(tmp_path / "web.sqlite3"), ImportOptions(dry_run=True)
    )
    assert report.dry_run and any(account.status == "imported" for account in report.accounts)
    assert await counts(harness) == before

    foreign = tmp_path / "other.sqlite3"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE web_users (id TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(LegacySchemaError):
        read_legacy_database(foreign)
    with pytest.raises(LegacySchemaError):
        read_legacy_database(tmp_path / "missing.sqlite3")


def run_cli(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items()}
    environment.pop("MIA_TEST_DATABASE_URL", None)
    environment["DATABASE_URL"] = database_url()
    return subprocess.run(
        [sys.executable, "scripts/migrate_web_sqlite.py", *arguments],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_cli_reports_json_without_personal_data(harness: Harness, tmp_path: Path) -> None:
    del harness  # migrated, truncated database
    build_legacy_database(tmp_path / "web.sqlite3")
    copy = copy_of(tmp_path / "web.sqlite3", tmp_path)
    first = await asyncio.to_thread(run_cli, ["--sqlite", str(copy)])
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert sorted(account["status"] for account in report["accounts"]) == [
        "imported",
        "imported",
        "skipped",
    ]
    assert "@example.test" not in first.stdout
    second = await asyncio.to_thread(run_cli, ["--sqlite", str(copy)])
    assert second.returncode == 0
    assert sorted(account["status"] for account in json.loads(second.stdout)["accounts"]) == [
        "skipped",
        "unchanged",
        "unchanged",
    ]
    missing = await asyncio.to_thread(run_cli, ["--sqlite", str(tmp_path / "nope.sqlite3")])
    assert missing.returncode == 2
