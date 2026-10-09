"""Timer, focus and alarm: deadlines in PluginState per installation, no firing here."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app.modules.plugins.builtin import CallIdentity, InMemoryPluginStateStore

from tests.plugins.support import (
    BASE_TIME,
    Caller,
    FakeClock,
    connect,
    make_services,
    meta,
    server_for,
)


def iso(clock: FakeClock, seconds: int) -> str:
    return (datetime.fromtimestamp(clock.now, UTC) + timedelta(seconds=seconds)).isoformat()


async def test_timer_start_get_pause_resume_cancel_and_state_is_persisted() -> None:
    clock = FakeClock()
    state = InMemoryPluginStateStore()
    async with connect(server_for("builtin.timer", make_services(clock, state=state))) as client:
        call = Caller(client, meta())
        started = await call("start", {"seconds": 300, "label": "чай"})
        item = started["changed"]
        assert item["state"] == "running" and item["remaining_ms"] == 300_000
        assert item["deadline_ms"] == int(BASE_TIME * 1000) + 300_000
        assert started["server_time_ms"] == int(BASE_TIME * 1000)
        assert "чай" in await call.text("get")

        clock.advance(100)
        got = await call("get")
        assert got["items"][0]["remaining_ms"] == 200_000

        paused = await call("pause")
        clock.advance(500)  # paused time does not count
        assert paused["changed"]["state"] == "paused"
        assert (await call("get"))["items"][0]["remaining_ms"] == 200_000

        resumed = await call("resume")
        assert resumed["changed"]["state"] == "running"
        assert resumed["changed"]["deadline_ms"] == int(clock.now * 1000) + 200_000

        clock.advance(201)
        expired = (await call("get"))["items"][0]
        assert expired["state"] == "expired" and expired["remaining_ms"] == 0
        assert "время вышло" in await call.text("get")

        await call("cancel")
        assert (await call("get"))["items"] == []
        assert "нет" in await call.text("get")

    # The state is what M16 scans: deadline in epoch ms in PluginState.
    snapshot = await state.load(CallIdentity("user-1", "ws-1", "inst-a"), "builtin.timer")
    assert snapshot.data == {"items": []}
    assert snapshot.version == 4  # start, pause, resume, cancel; reads never write


async def test_timer_state_survives_a_new_server_and_client() -> None:
    clock = FakeClock()
    state = InMemoryPluginStateStore()
    async with connect(server_for("builtin.timer", make_services(clock, state=state))) as client:
        await Caller(client, meta())("start", {"seconds": 60, "label": "a"})
    async with connect(server_for("builtin.timer", make_services(clock, state=state))) as client:
        items = (await Caller(client, meta())("get"))["items"]
    assert [item["label"] for item in items] == ["a"]


async def test_installations_are_isolated() -> None:
    clock = FakeClock()
    services = make_services(clock)
    async with connect(server_for("builtin.timer", services)) as client:
        a = Caller(client, meta(installation="inst-a"))
        b = Caller(client, meta(installation="inst-b"))
        other_workspace = Caller(client, meta(installation="inst-a", workspace="ws-2"))
        await a("start", {"seconds": 60, "label": "mine"})
        assert (await b("get"))["items"] == []
        assert (await other_workspace("get"))["items"] == []
        assert len((await a("get"))["items"]) == 1
        await b("start", {"seconds": 30, "label": "theirs"})
        assert [i["label"] for i in (await a("get"))["items"]] == ["mine"]


async def test_timer_several_timers_need_an_id_and_have_a_limit() -> None:
    clock = FakeClock()
    async with connect(server_for("builtin.timer", make_services(clock))) as client:
        call = Caller(client, meta())
        first = (await call("start", {"seconds": 60, "label": "a"}))["changed"]["id"]
        second = (await call("start", {"seconds": 90, "label": "b"}))["changed"]["id"]
        assert first != second
        message = await call.error("cancel")
        assert "Уточните id" in message and first in message and second in message
        await call("cancel", {"id": first})
        assert [i["id"] for i in (await call("get"))["items"]] == [second]
        assert "не найдена" in await call.error("cancel", {"id": "zzz"})
        await call("cancel")  # now unambiguous
        for index in range(10):
            await call("start", {"seconds": 60, "label": f"t{index}"})
        assert "максимум 10" in await call.error("start", {"seconds": 60, "label": "extra"})


async def test_timer_rejects_bad_arguments_and_wrong_state_transitions() -> None:
    async with connect(server_for("builtin.timer", make_services())) as client:
        call = Caller(client, meta())
        assert await call.error("start", {"seconds": 0, "label": "x"})
        assert await call.error("start", {"seconds": 86401, "label": "x"})
        assert await call.error("start", {"seconds": 10, "label": ""})
        assert "Подходящих записей нет" in await call.error("pause")
        await call("start", {"seconds": 60, "label": "x"})
        assert "Подходящих записей нет" in await call.error("resume")
        await call("pause")
        assert "Подходящих записей нет" in await call.error("pause")


async def test_concurrent_calls_do_not_lose_updates() -> None:
    async with connect(server_for("builtin.notes", make_services())) as client:
        call = Caller(client, meta())
        await asyncio.gather(*(call("add", {"text": f"n{i}"}) for i in range(15)))
        assert len((await call("list"))["notes"]) == 15


async def test_focus_is_a_single_slot_that_start_replaces() -> None:
    clock = FakeClock()
    async with connect(server_for("builtin.focus", make_services(clock))) as client:
        call = Caller(client, meta())
        default = await call("start")
        assert default["changed"]["remaining_ms"] == 1_500_000
        assert default["changed"]["label"] == "Фокус"
        replaced = await call("start", {"seconds": 300, "label": "Перерыв"})
        assert [i["label"] for i in replaced["items"]] == ["Перерыв"]
        assert await call.error("start", {"seconds": 10801})
        await call("pause")
        await call("resume")
        await call("cancel")
        assert (await call("get"))["items"] == []


async def test_alarm_set_get_cancel_with_timezone() -> None:
    clock = FakeClock()
    state = InMemoryPluginStateStore()
    async with connect(server_for("builtin.alarm", make_services(clock, state=state))) as client:
        call = Caller(client, meta())
        at = iso(clock, 3600)
        result = await call("set", {"at": at, "label": "подъём"})
        item = result["changed"]
        assert item["state"] == "scheduled" and item["remaining_ms"] == 3_600_000
        assert item["deadline_ms"] == int(BASE_TIME * 1000) + 3_600_000
        assert item["at"] == at
        assert "подъём" in await call.text("get")
        offset = (datetime.fromtimestamp(clock.now, UTC) + timedelta(hours=2)).astimezone(
            datetime.now().astimezone().tzinfo
        )
        await call("set", {"at": offset.isoformat(), "label": "второй"})
        assert len((await call("get"))["items"]) == 2
        clock.advance(3601)
        assert (await call("get"))["items"][0]["state"] == "expired"
        await call("cancel", {"cancel_all": True})
        assert (await call("get"))["items"] == []
    snapshot = await state.load(CallIdentity("user-1", "ws-1", "inst-a"), "builtin.alarm")
    assert snapshot.data["items"] == []


@pytest.mark.parametrize(
    ("at", "needle"),
    [
        ("завтра в 7", "ISO 8601"),
        ("2026-10-05T14:00:00", "часовой пояс"),
        ("2020-01-01T00:00:00+00:00", "в будущем"),
        ("2099-01-01T00:00:00+00:00", "31 день"),
    ],
)
async def test_alarm_rejects_ambiguous_or_out_of_range_times(at: str, needle: str) -> None:
    async with connect(server_for("builtin.alarm", make_services())) as client:
        message = await Caller(client, meta()).error("set", {"at": at, "label": "x"})
        assert needle in message


async def test_alarm_isolated_per_installation() -> None:
    clock = FakeClock()
    async with connect(server_for("builtin.alarm", make_services(clock))) as client:
        a = Caller(client, meta(installation="a"))
        b = Caller(client, meta(installation="b"))
        await a("set", {"at": iso(clock, 600), "label": "x"})
        assert (await b("get"))["items"] == []
