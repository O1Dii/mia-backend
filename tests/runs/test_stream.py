"""Reconnect by cursor without loss or duplicates; 20 parallel runs without mixing."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from app.modules.runs import InvalidRunCursorError, NewRun, RunCursor, RunService

from tests.fakes import assert_mvp_valid
from tests.runs.conftest import RunWorld, assert_well_formed, collect, types
from tests.runs.gateways import EchoGateway, GatedGateway


async def take(
    service: RunService, user: uuid.UUID, run_id: uuid.UUID, count: int, *, after: int = 0
) -> list[dict[str, Any]]:
    """Read ``count`` events, then drop the connection (close the iterator)."""
    taken: list[dict[str, Any]] = []
    stream = service.events(user_id=user, run_id=run_id, after_sequence=after)
    try:
        async with asyncio.timeout(5):
            async for event in stream:
                if event is not None:
                    taken.append(event.to_wire())
                if len(taken) == count:
                    break
    finally:
        await stream.aclose()
    return taken


async def test_reconnect_by_cursor_mid_run_loses_and_duplicates_nothing(world: RunWorld) -> None:
    chunks = [f"часть {index}. " for index in range(8)]
    gateway = GatedGateway(chunks)
    service = world.service(gateway, delta_flush_chars=1)
    record, _ = await service.start(NewRun(user_id=world.user, profile="chat", input_text="Go"))

    for _ in range(3):
        gateway.permits.release()
    first = await take(service, world.user, record.id, 3)
    assert [event["sequence"] for event in first] == [1, 2, 3]

    for _ in range(3):
        gateway.permits.release()
    await asyncio.sleep(0.05)  # events are produced while the client is away
    resume = RunCursor.decode(first[-1]["cursor"], run_id=record.id).sequence
    second = await take(service, world.user, record.id, 2, after=resume)

    for _ in range(len(chunks)):
        gateway.permits.release()
    resume = RunCursor.decode(second[-1]["cursor"], run_id=record.id).sequence
    rest = await collect(service, world.user, record.id, after=resume)

    events = first + second + rest
    assert_well_formed(events)
    deltas = "".join(e["payload"]["text"] for e in events if e["type"] == "message.delta")
    assert deltas == "".join(chunks)
    assert types(events)[-2:] == ["response.final", "run.completed"]


async def test_resume_after_completion_and_from_terminal_cursor(world: RunWorld) -> None:
    service = world.service(EchoGateway(), delta_flush_chars=1)
    record, _ = await service.start(NewRun(user_id=world.user, profile="chat", input_text="abc"))
    full = await collect(service, world.user, record.id)

    tail = await collect(service, world.user, record.id, after=full[4]["sequence"])
    assert tail == full[5:]
    assert await take(service, world.user, record.id, 1, after=full[-1]["sequence"]) == []


async def test_idle_stream_sends_keepalive(world: RunWorld) -> None:
    gateway = GatedGateway(["a"])
    service = world.service(gateway)
    record, _ = await service.start(NewRun(user_id=world.user, profile="chat", input_text="x"))
    seen: list[str | None] = []
    async with asyncio.timeout(5):
        async for event in service.events(user_id=world.user, run_id=record.id):
            seen.append(None if event is None else event.type)
            if event is None:
                gateway.permits.release()
            elif event.terminal:
                break
    assert None in seen and seen[-1] == "run.completed"


def test_cursor_is_opaque_and_bound_to_its_run() -> None:
    run_id, other = uuid.uuid4(), uuid.uuid4()
    cursor = RunCursor(run_id=run_id, sequence=7).encode()

    assert RunCursor.decode(cursor, run_id=run_id).sequence == 7
    assert len(cursor) <= 512 and cursor.isascii()
    for bad in [cursor, "", "x" * 600, "%%%", "eyJ2IjoxfQ"]:
        with pytest.raises(InvalidRunCursorError):
            RunCursor.decode(bad, run_id=other)


async def test_twenty_parallel_runs_do_not_mix_events(world: RunWorld) -> None:
    service = world.service(EchoGateway(chunk=2), delta_flush_chars=1)
    users = world.users

    async def one(index: int) -> tuple[uuid.UUID, str, list[dict[str, Any]]]:
        user = users[index % len(users)]
        text = f"запрос-{index:02d}"
        record, _ = await service.start(NewRun(user_id=user, profile="chat", input_text=text))
        return record.id, text, await collect(service, user, record.id)

    results = await asyncio.gather(*(one(index) for index in range(20)))

    assert len({run_id for run_id, _, _ in results}) == 20
    for run_id, text, events in results:
        assert_well_formed(events)
        assert {event["run_id"] for event in events} == {str(run_id)}
        deltas = "".join(e["payload"]["text"] for e in events if e["type"] == "message.delta")
        assert deltas == f"Ответ {text}."
        final = next(e for e in events if e["type"] == "response.final")
        assert final["payload"]["response"]["speech"] == f"Ответ {text}."
        for event in events:
            assert_mvp_valid(event, "run-events")
