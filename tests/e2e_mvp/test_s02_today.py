"""Scenario 2: «Что у меня сегодня» on the available plugins (Linear, notes, timers).

The production composition: Linear (real package, recorded tool set, OAuth connection),
``builtin.notes`` and ``builtin.timer`` from the store. A note and a running timer are seeded
through the plugins' own tools (tile tool calls, no model). The question is asked from the
browser (chat) and from the phone (voice); the scripted model calls the tools the router
offers. Google Calendar (M42, optional/PARTIAL) is not part of the MVP set and is not
installed here. The real model's choice of tools is a live check (UC-M73-1).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from app.core.ports.model_gateway import ModelDone, ModelEvent, ModelToolCall, ToolCallRequested
from app.modules.plugins.catalog.package import load_package

from tests.e2e_mvp.harness import Mvp, Person, final, payloads, requires_stack, say, types

pytestmark = requires_stack

LINEAR = "team.linear.list_issues"
NOTES = "builtin.notes.list"
TIMER = "builtin.timer.get"
TODAY = (LINEAR, NOTES, TIMER)
QUESTION = "Что у меня сегодня?"


async def prepare(mvp: Mvp, tmp_path: Path) -> tuple[Person, Person, dict[str, str]]:
    """Linear connected, notes and timer installed, one note and one running timer."""
    published = await mvp.catalog.publish(
        load_package(mvp.linear_directory(tmp_path)),
        actor_user_id=mvp.admin_id,
        discover_token=mvp.auth_server.mint_token(),
    )
    assert published.created
    await mvp.publish_builtin("notes")
    await mvp.publish_builtin("timer")

    phone = await mvp.phone("today")
    await phone.register_device_tools()
    linear = await mvp.install(phone, "team.linear")
    callback = await mvp.connect(phone, linear["id"])
    assert callback.status_code in (200, 302, 303), callback.text
    notes = await mvp.install(phone, "builtin.notes")
    timer = await mvp.install(phone, "builtin.timer")
    assert notes["status"] == timer["status"] == "active"
    ids = {"linear": linear["id"], "notes": notes["id"], "timer": timer["id"]}

    added = await phone.post(
        f"/v1/installations/{ids['notes']}/tiles/notes/tool-calls",
        {"name": "add", "arguments": {"text": "Купить подарок Яне"}},
    )
    assert added.status_code == 200, added.text
    assert added.json()["status"] == "succeeded", added.json()
    started = await phone.post(
        f"/v1/installations/{ids['timer']}/tiles/active-timers/tool-calls",
        {"name": "start", "arguments": {"seconds": 1500}},
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "succeeded", started.json()

    web = await mvp.browser("today")
    assert web.user_id == phone.user_id
    return phone, web, ids


def three_at_once(request: Any) -> Sequence[ModelEvent]:
    """One model step asking for Linear, notes and timer at once (parallel tool calls)."""
    names = {tool.name for tool in request.tools}
    missing = set(TODAY) - names
    assert not missing, (missing, sorted(names))
    events: list[ModelEvent] = []
    arguments: dict[str, dict[str, Any]] = {LINEAR: {"assignee": "me"}, NOTES: {}, TIMER: {}}
    for index, name in enumerate(TODAY):
        events.append(ToolCallRequested(ModelToolCall(f"today_{index}", name, arguments[name])))
    events.append(ModelDone("tool_calls"))
    return events


BRIEF = "Сегодня: две задачи в Linear, заметка «Купить подарок Яне» и таймер на 25 минут."
DISPLAY = {
    "kind": "card",
    "title": "Сегодня",
    "markdown": "- ENG-101, ENG-102\n- Купить подарок Яне\n- Таймер: 25 мин",
}


async def test_today_in_chat_reads_linear_notes_and_timer(mvp: Mvp, tmp_path: Path) -> None:
    phone, web, ids = await prepare(mvp, tmp_path)

    mvp.model.then(three_at_once, say(BRIEF, DISPLAY))
    events = await web.run(QUESTION, profile="chat", on_device=False)

    assert types(events)[-1] == "run.completed", types(events)
    started = [p["tool"] for p in payloads(events, "tool.call.started")]
    assert started == list(TODAY)
    completed = payloads(events, "tool.call.completed")
    assert [c["status"] for c in completed] == ["succeeded"] * 3, completed
    response = final(events)
    assert response["speech"] == BRIEF and response["display"]["kind"] == "card"
    # What the model read back: the three results (untrusted data), all three sources.
    tool_texts = "\n".join(mvp.model.tool_messages(1))
    assert "ENG-101" in tool_texts and "Купить подарок Яне" in tool_texts
    # M73: a short question is answered directly; the background tool and search are offered.
    offered = mvp.model.offered(0)
    assert "background.start" in offered
    assert any(name.startswith("builtin.internet.") for name in offered)
    assert "job.created" not in types(events)

    # «Сейчас» shows the running timer with how to cancel it (no model).
    now = await web.get("/v1/now")
    assert now.status_code == 200, now.text
    timers = [i for i in now.json()["items"] if i["kind"] == "timer"]
    assert len(timers) == 1, now.json()
    assert timers[0]["installation_id"] == ids["timer"]
    # The phone sees the same strip (one account on web and Android).
    phone_now = (await phone.get("/v1/now")).json()["items"]
    assert [i["id"] for i in phone_now] == [i["id"] for i in now.json()["items"]]


async def test_today_by_voice_reads_two_sources_within_the_voice_budget(
    mvp: Mvp, tmp_path: Path
) -> None:
    """Current behaviour: the voice profile allows 2 tool calls (``RUN_VOICE_MAX_TOOL_CALLS``),
    so the third source of the same question is skipped (``tool_budget_exceeded``)."""
    phone, _, _ = await prepare(mvp, tmp_path)
    assert mvp.settings.run_voice_max_tool_calls == 2 and mvp.settings.run_voice_max_steps == 3

    mvp.model.then(three_at_once, say(BRIEF))
    events = await phone.run(QUESTION, profile="voice")

    assert types(events)[-1] == "run.completed", types(events)
    started = [p["tool"] for p in payloads(events, "tool.call.started")]
    assert started == [LINEAR, NOTES]
    assert [c["status"] for c in payloads(events, "tool.call.completed")] == ["succeeded"] * 2
    skipped = mvp.model.tool_messages(1)[-1]
    assert "tool_budget_exceeded" in skipped
    # The voice tool set: only manifest ``voice: true`` tools of the three plugins.
    offered = mvp.model.offered(0)
    assert set(TODAY) <= offered and "builtin.notes.delete" not in offered
    assert final(events)["speech"] == BRIEF


@pytest.mark.xfail(
    strict=True,
    reason="M60 finding: voice budget of 2 tool calls cannot cover Linear + notes + timer "
    "in one «Что у меня сегодня?» (repair M12-R1 / decision)",
)
async def test_today_by_voice_reads_all_three_sources(mvp: Mvp, tmp_path: Path) -> None:
    phone, _, _ = await prepare(mvp, tmp_path)
    mvp.model.then(three_at_once, say(BRIEF))
    events = await phone.run(QUESTION, profile="voice")
    statuses = [c["status"] for c in payloads(events, "tool.call.completed")]
    assert statuses == ["succeeded"] * 3
