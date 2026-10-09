"""``/v1/memory*`` over HTTP: owner-only, bearer and cookie, export/import, settings, erase."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.config import Settings
from app.core.database import Database
from app.core.redis import RedisClient
from app.core.runtime import Runtime
from app.modules.memory import MemoryConfig, install_memory
from app.modules.models.vault import KeyVault

from tests.fakes import assert_mvp_valid
from tests.memory.conftest import KEYRING
from tests.runs.conftest import requires_postgres
from tests.workspace.harness import (
    Harness,
    android_login,
    build_harness,
    database_url,
    key,
    web_headers,
    web_login,
)

pytestmark = requires_postgres


@asynccontextmanager
async def memory_app() -> AsyncIterator[Harness]:
    async with build_harness() as harness:
        settings = Settings(app_env="test")
        database = Database(database_url())
        runtime = Runtime(database=database, redis=RedisClient(settings.redis_url))
        install_memory(
            harness.app,
            runtime,
            vault=KeyVault(KEYRING),
            config=MemoryConfig(retention_sweep_s=0),
        )
        try:
            yield harness
        finally:
            await database.close()


async def test_facts_people_round_trip_and_owner_only() -> None:
    async with memory_app() as harness, harness.client() as android, harness.client() as web:
        auth = await android_login(android, "sub-memory-owner")
        created = await android.post(
            "/v1/memory/facts",
            headers={**auth, **key()},
            json={"text": "Яна — моя жена", "category": "home"},
        )
        assert created.status_code == 201, created.text
        fact = created.json()
        assert_mvp_valid(fact, "memory", "Fact")
        assert fact["status"] == "active"
        people = (await android.get("/v1/memory/people", headers=auth)).json()["items"]
        assert [(p["display_name"], p["relation"]) for p in people] == [("Яна", "жена")]

        listed = await android.get("/v1/memory/facts?status=active", headers=auth)
        assert [f["id"] for f in listed.json()["items"]] == [fact["id"]]
        assert listed.json()["next_cursor"] is None

        no_key = await android.patch(
            f"/v1/memory/facts/{fact['id']}", headers=auth, json={"text": "x"}
        )
        assert no_key.status_code == 422
        patched = await android.patch(
            f"/v1/memory/facts/{fact['id']}",
            headers={**auth, **key()},
            json={"scope": "automation", "category": "profile"},
        )
        assert patched.json()["scope"] == "automation"
        assert patched.json()["category"] == "profile"

        person = await android.post(
            "/v1/memory/people",
            headers={**auth, **key()},
            json={
                "display_name": "Олег",
                "relation": "коллега",
                "identifiers": {"phone": "+375291112233", "telegram_username": "@oleg_p"},
                "trusted": True,
            },
        )
        assert person.status_code == 201
        body = person.json()
        assert_mvp_valid(body, "memory", "Person")
        assert body["identifiers"] == {"phone": "+375291112233", "telegram_username": "oleg_p"}
        assert body["trusted"] is True

        # Web cookie of another account: everything is 404/empty.
        await web_login(web, "sub-memory-stranger")
        assert (await web.get("/v1/memory/facts")).json()["items"] == []
        foreign = await web.patch(
            f"/v1/memory/facts/{fact['id']}", headers=web_headers(), json={"status": "rejected"}
        )
        assert (foreign.status_code, foreign.json()["error"]["code"]) == (
            404,
            "resource_not_found",
        )
        gone = await web.delete(f"/v1/memory/people/{body['id']}", headers=web_headers())
        assert gone.status_code == 404

        # The owner on the web sees the same memory and can delete.
        await web_login(web, "sub-memory-owner")
        assert len((await web.get("/v1/memory/people")).json()["items"]) == 2
        deleted = await web.delete(f"/v1/memory/people/{body['id']}", headers=web_headers())
        assert deleted.status_code == 204
        async with harness.client() as anonymous:
            assert (await anonymous.get("/v1/memory/facts")).status_code == 401


async def test_settings_preferences_context_and_access_log() -> None:
    async with memory_app() as harness, harness.client() as android:
        auth = await android_login(android, "sub-memory-settings")
        settings = await android.get("/v1/memory/settings", headers=auth)
        assert settings.json() == {"autosave_categories": [], "history_retention": "forever"}
        sensitive = await android.put(
            "/v1/memory/settings",
            headers={**auth, **key()},
            json={"autosave_categories": ["finance"]},
        )
        assert sensitive.status_code == 422
        updated = await android.put(
            "/v1/memory/settings",
            headers={**auth, **key()},
            json={"autosave_categories": ["interests"], "history_retention": "30d"},
        )
        assert updated.json() == {
            "autosave_categories": ["interests"],
            "history_retention": "30d",
        }
        pref = await android.put(
            "/v1/memory/preferences/reply.language", headers={**auth, **key()}, json={"value": "ru"}
        )
        assert_mvp_valid(pref.json(), "memory", "Preference")
        bad = await android.put(
            "/v1/memory/preferences/Bad-Key", headers={**auth, **key()}, json={"value": 1}
        )
        assert bad.status_code == 422

        await android.post(
            "/v1/memory/facts",
            headers={**auth, **key()},
            json={"text": "Пьёт кофе без сахара, звонить +79161234567", "category": "interests"},
        )
        context = await android.get("/v1/memory/context?query=кофе", headers=auth)
        assert context.json()["context"] == (
            "- [interests] Пьёт кофе без сахара, звонить [номер скрыт]"
        )
        log = (await android.get("/v1/memory/access-log", headers=auth)).json()["items"]
        assert [entry["action"] for entry in log[:2]] == ["context", "create"]
        assert all("text" not in entry for entry in log)


async def test_export_import_and_forget_everything() -> None:
    async with memory_app() as harness, harness.client() as a, harness.client() as b:
        auth_a = await android_login(a, "sub-memory-export")
        auth_b = await android_login(b, "sub-memory-import")
        for text, category in (("Меня зовут Алексей", "profile"), ("Яна — моя жена", "home")):
            await a.post(
                "/v1/memory/facts",
                headers={**auth_a, **key()},
                json={"text": text, "category": category},
            )
        archive = await a.get("/v1/memory/export", headers=auth_a)
        assert archive.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            names = set(zipped.namelist())
            assert {"memories/profile.md", "memories/home.md", "memories/people.md"} <= names
            assert "- Меня зовут Алексей" in zipped.read("memories/profile.md").decode()
            assert "**Яна** — жена" in zipped.read("memories/people.md").decode()
            exported = json.loads(zipped.read("memory.json"))
        for record in exported["facts"] + exported["people"]:
            assert_mvp_valid(record, "memory")
        as_json = (await a.get("/v1/memory/export?format=json", headers=auth_a)).json()
        assert len(as_json["facts"]) == 2

        idem = key()
        payload = {"format": "json", "source": "mia", "content": json.dumps(as_json)}
        imported = await b.post("/v1/memory/import", headers={**auth_b, **idem}, json=payload)
        assert imported.status_code == 201
        assert (imported.json()["facts"], imported.json()["people"]) == (2, 1)
        replay = await b.post("/v1/memory/import", headers={**auth_b, **idem}, json=payload)
        assert replay.json() == imported.json()
        queue = (await b.get("/v1/memory/facts?status=suggested", headers=auth_b)).json()
        assert {f["text"] for f in queue["items"]} == {"Меня зовут Алексей", "Яна — моя жена"}
        assert all(f["source"]["type"] == "import" for f in queue["items"])
        people = (await b.get("/v1/memory/people?status=suggested", headers=auth_b)).json()
        assert [p["display_name"] for p in people["items"]] == ["Яна"]

        markdown = "# Работа\n- Работает в Минске\n- Пишет на Kotlin\n\nПросто текст\n"
        md = await b.post(
            "/v1/memory/import",
            headers={**auth_b, **key()},
            json={"format": "markdown", "source": "chatgpt", "content": markdown},
        )
        assert md.json()["facts"] == 2
        empty = await b.post(
            "/v1/memory/import",
            headers={**auth_b, **key()},
            json={"format": "json", "content": "not json"},
        )
        assert (empty.status_code, empty.json()["error"]["code"]) == (422, "memory_import_invalid")

        erased = await a.delete("/v1/memory", headers={**auth_a, **key()})
        assert erased.status_code == 204
        after = (await a.get("/v1/memory/export?format=json", headers=auth_a)).json()
        assert (after["facts"], after["people"], after["preferences"]) == ([], [], [])
        # The other account kept its memory.
        assert len((await b.get("/v1/memory/facts", headers=auth_b)).json()["items"]) == 4
