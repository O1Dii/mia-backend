"""``GET /v1/now``: active timers, alarms, focus and jobs of the account (PostgreSQL)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.core.config import Settings
from app.modules.jobs.composition import build_jobs
from app.modules.jobs.now import NOW_PLUGINS, plugin_item
from app.modules.workspace.models import Installation, PluginState, Workspace
from sqlalchemy import select

from tests.chats.conftest import ChatWorld, _Runtime, check, create_chat
from tests.jobs.conftest import jobs_settings
from tests.workspace.harness import requires_postgres

NOW_MS = int(datetime.now(UTC).timestamp() * 1000)


def item(
    item_id: str, label: str, status: str, deadline: int | None, **extra: Any
) -> dict[str, Any]:
    return {
        "id": item_id,
        "label": label,
        "status": status,
        "deadline_ms": deadline,
        "remaining_ms": extra.pop("remaining_ms", 0 if deadline is None else deadline - NOW_MS),
        "duration_ms": extra.pop("duration_ms", 600_000),
        "created_ms": NOW_MS - 60_000,
        **extra,
    }


async def install(
    world: ChatWorld, user: uuid.UUID, plugin_id: str, items: list[dict[str, Any]], status: str
) -> uuid.UUID:
    async with world.factory() as session, session.begin():
        workspace = await session.scalar(select(Workspace).where(Workspace.owner_user_id == user))
        if workspace is None:
            workspace = Workspace(id=uuid.uuid4(), owner_user_id=user)
            session.add(workspace)
            await session.flush()
        installation = Installation(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            plugin_id=plugin_id,
            version="1.0.0",
            status=status,
            source="catalog",
        )
        session.add(installation)
        session.add(
            PluginState(workspace_id=workspace.id, plugin_id=plugin_id, data={"items": items})
        )
        return installation.id


@requires_postgres
async def test_now_lists_active_timers_alarms_and_jobs(world: ChatWorld) -> None:
    stack = build_jobs(
        Settings(app_env="test"),
        cast(Any, _Runtime(world.factory)),
        runs=world.runs,
        jobs_settings=jobs_settings(),
    )
    async with world.harness.client() as client, world.harness.client() as other:
        headers, user = await world.login(client, "alice")
        stranger, bob = await world.login(other, "bob")
        empty = (await client.get("/v1/now", headers=headers)).json()
        check("NowPage", empty)
        assert empty["items"] == []

        timers = await install(
            world,
            user,
            "builtin.timer",
            [
                item("t1", "Паста", "running", NOW_MS + 7_200_000),
                item("t2", "Чай", "running", NOW_MS + 3_600_000),
                item("t3", "Пауза", "paused", None, remaining_ms=120_000),
                item("t4", "Истёк", "running", NOW_MS - 1_000),
            ],
            "active",
        )
        alarms = await install(
            world,
            user,
            "builtin.alarm",
            [item("a1", "Подъём", "scheduled", NOW_MS + 8 * 3600_000)],
            "active",
        )
        await install(
            world,
            user,
            "builtin.focus",
            [item("f1", "Скрыт", "running", NOW_MS + 60_000)],
            "disabled",
        )
        await install(
            world,
            bob,
            "builtin.timer",
            [item("b1", "Чужой", "running", NOW_MS + 900_000)],
            "active",
        )

        chat = uuid.UUID((await create_chat(client, headers))["id"])
        events = await world.ask(user, "Сделай в фоне", conversation_id=chat)
        from_chat, _ = await stack.service.create_job(
            user_id=user,
            request={"goal": "Сводка"},
            origin_type="run",
            origin_id=events[0]["run_id"],
        )
        from_api, _ = await stack.service.create_job(
            user_id=user, request={"goal": "Из API"}, origin_type="user", origin_id=str(user)
        )

        body = (await client.get("/v1/now", headers=headers)).json()
        check("NowPage", body)
        assert [(i["kind"], i["id"]) for i in body["items"]] == [
            ("timer", "t2"),
            ("timer", "t1"),
            ("alarm", "a1"),
            ("timer", "t3"),
            ("job", str(from_api.id)),
            ("job", str(from_chat.id)),
        ]
        first = body["items"][0]
        assert first["label"] == "Чай" and 0 < first["remaining_ms"] <= 3_600_000
        assert first["cancel"] == {
            "type": "tile_tool",
            "installation_id": str(timers),
            "tile_id": "active-timers",
            "name": "cancel",
            "arguments": {"id": "t2"},
        }
        alarm = body["items"][2]
        assert alarm["state"] == "scheduled" and alarm["cancel"]["tile_id"] == "alarms"
        assert alarm["installation_id"] == str(alarms)
        paused = body["items"][3]
        assert (paused["state"], paused["deadline_at"], paused["remaining_ms"]) == (
            "paused",
            None,
            120_000,
        )
        jobs = {i["id"]: i for i in body["items"] if i["kind"] == "job"}
        assert jobs[str(from_chat.id)]["conversation_id"] == str(chat)
        assert jobs[str(from_api.id)]["conversation_id"] is None
        assert jobs[str(from_chat.id)]["cancel"] == {"type": "job", "job_id": str(from_chat.id)}
        assert jobs[str(from_chat.id)]["state"] == "queued"

        bobs = (await other.get("/v1/now", headers=stranger)).json()
        assert [i["id"] for i in bobs["items"]] == ["b1"]
        assert (await client.get("/v1/now")).status_code == 401


def test_tile_ids_match_the_builtin_manifests() -> None:
    for plugin_id, plugin in NOW_PLUGINS.items():
        folder = plugin_id.removeprefix("builtin.")
        manifest = json.loads(Path(f"app/plugins/builtin/{folder}/mia.json").read_text("utf-8"))
        tiles = {tile["id"] for tile in manifest["surfaces"]["tiles"]}
        assert plugin.tile_id in tiles, plugin_id


def test_malformed_items_are_skipped() -> None:
    installation = uuid.uuid4()
    for raw in (None, "x", {"id": "1", "status": "done"}, {"status": "running", "deadline_ms": 1}):
        assert plugin_item("builtin.timer", installation, raw, NOW_MS) is None
    assert (
        plugin_item("builtin.notes", installation, item("n", "n", "running", NOW_MS + 1), NOW_MS)
        is None
    )
    paused = item("p", "p", "paused", None, remaining_ms=0)
    assert plugin_item("builtin.timer", installation, paused, NOW_MS) is None
