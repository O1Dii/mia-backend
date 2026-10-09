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
from app.web_poc.accounts import AccountStore
from app.web_poc.runtime import Workspace as LegacyWorkspace
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


def build_legacy_database(path: Path) -> dict[str, str]:
    """Create a web-POC database with the POC's own store; return legacy user ids."""
    store = AccountStore(str(path))
    try:
        _, alice = store.login(identity("google-alice", "alice@example.test"))
        _, bob = store.login(identity("google-bob", "bob@example.test"))
        _, hub = store.login(identity("hub:7f6c1d3e-2b1a-4c5d-9e8f-0a1b2c3d4e5f", "hub@ex.test"))
        workspace = LegacyWorkspace(expires=time.monotonic() + 60, subject_id=alice["id"])
        workspace.install("mia.timer")
        workspace.install("mia.notes")
        workspace.states["mia.timer"] = {"left": 42}
        workspace.states["mia.notes"] = {"items": ["купить хлеб"]}
        workspace.audit.append({"action": "secret-ish audit"})
        store.save(workspace)
        hub_workspace = LegacyWorkspace(expires=time.monotonic() + 60, subject_id=hub["id"])
        hub_workspace.install("mia.timer")
        store.save(hub_workspace)
        store.review("mia.timer", alice["id"], 5, "Отличный таймер")
        store.review("mia.timer", bob["id"], 3, "Нормально")
    finally:
        store.close()
    return {"alice": alice["id"], "bob": bob["id"], "hub": hub["id"]}


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
