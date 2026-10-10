"""M77 routing: the user's settings × event type × explicit ``delivery.channels``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.modules.jobs.domain import NewJob
from app.modules.notifications.channels import DeliveryResult, OutboundMessage
from app.modules.notifications.models import DeliveryRow, InboxItemRow
from app.modules.notifications.preferences import (
    EVENT_TYPES,
    PreferencesError,
)
from sqlalchemy import select

from tests.fakes import assert_mvp_valid
from tests.jobs.conftest import JobsWorld, requires_postgres
from tests.jobs.test_schedules import _item, _plugin_state
from tests.runs.gateways import scripted

pytestmark = requires_postgres


class FakeChannel:
    def __init__(self, name: str, *, label: str | None = None) -> None:
        self.name = name
        if label is not None:
            self.label = label
        self.messages: list[OutboundMessage] = []

    async def deliver(self, message: OutboundMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult("sent")


def stack_of(world: JobsWorld, *, push: bool = True):  # type: ignore[no-untyped-def]
    return world.stack(world.run_service(scripted()), push=push)


async def deliveries(world: JobsWorld) -> list[tuple[str, str, str | None]]:
    """``(item title, channel, status)`` of every delivery, by title (the test clock is frozen)."""
    async with world.factory() as session:
        rows = (
            await session.execute(
                select(InboxItemRow.title, DeliveryRow.channel, DeliveryRow.status)
                .join(DeliveryRow, DeliveryRow.inbox_item_id == InboxItemRow.id)
                .order_by(InboxItemRow.title, DeliveryRow.channel)
            )
        ).all()
    return [(title, channel, status) for title, channel, status in rows]


async def inbox_titles(world: JobsWorld) -> list[str]:
    async with world.factory() as session:
        return list((await session.scalars(select(InboxItemRow.title))).all())


# -- settings × event type (no explicit channels) --------------------------------------------


async def test_a_new_user_gets_the_inbox_and_push_only(world: JobsWorld) -> None:
    preferences = stack_of(world).notifications.preferences
    for kind in EVENT_TYPES:
        assert await preferences.route(world.user, kind) == ("push",)
    snapshot = await preferences.snapshot(world.user)
    assert_mvp_valid(snapshot, "notifications", "NotificationSettings")
    by_name = {channel["name"]: channel for channel in snapshot["channels"]}
    assert (by_name["push"]["enabled"], by_name["push"]["decided"]) == (True, False)
    assert (by_name["telegram"]["enabled"], by_name["telegram"]["decided"]) == (False, False)
    assert by_name["telegram"]["status"] == "not_connected"
    assert by_name["telegram"]["detail"] == "not_configured"  # no bot registered in this stack
    assert snapshot["inbox_always"] is True


@pytest.mark.parametrize(
    ("channels", "matrix", "expected"),
    [
        ({}, {}, {k: ("push",) for k in EVENT_TYPES}),
        ({"telegram": True}, {}, {k: ("push", "telegram") for k in EVENT_TYPES}),
        ({"push": False}, {}, {k: () for k in EVENT_TYPES}),
        (
            {"telegram": True},
            {"job_result": {"telegram": False}, "plugin_message": {"push": False}},
            {
                "timer_alarm": ("push", "telegram"),
                "job_result": ("push",),
                "approval": ("push", "telegram"),
                "plugin_message": ("telegram",),
            },
        ),
        # A cell is ignored while its channel is switched off.
        (
            {"telegram": False},
            {"timer_alarm": {"telegram": True}},
            {k: ("push",) for k in EVENT_TYPES},
        ),
        # A plugin channel is a channel like any other.
        (
            {"notify-board": True},
            {"approval": {"notify-board": False}},
            {
                "timer_alarm": ("push", "notify-board"),
                "job_result": ("push", "notify-board"),
                "approval": ("push",),
                "plugin_message": ("push", "notify-board"),
            },
        ),
    ],
)
async def test_routing_follows_switches_and_the_event_type_table(
    world: JobsWorld,
    channels: dict[str, bool],
    matrix: dict[str, dict[str, bool]],
    expected: dict[str, tuple[str, ...]],
) -> None:
    stack = stack_of(world)
    stack.notifications.channels.register(FakeChannel("notify-board"))
    preferences = stack.notifications.preferences
    await preferences.update(world.user, channels=channels, matrix=matrix)
    for kind in EVENT_TYPES:
        assert await preferences.route(world.user, kind) == expected[kind], kind
    # Another user is not affected.
    for kind in EVENT_TYPES:
        assert await preferences.route(world.stranger, kind) == ("push",)


async def test_unknown_names_are_rejected_as_a_whole(world: JobsWorld) -> None:
    preferences = stack_of(world).notifications.preferences
    with pytest.raises(PreferencesError) as channel:
        await preferences.update(world.user, channels={"telegram": True, "fax": True})
    assert channel.value.code == "notification_channel_unknown"
    with pytest.raises(PreferencesError) as kind:
        await preferences.update(world.user, matrix={"weather": {"push": False}})
    assert kind.value.code == "notification_event_type_unknown"
    with pytest.raises(PreferencesError):
        await preferences.update(world.user, channels={"inbox": False})
    snapshot = await preferences.snapshot(world.user)
    assert [c["decided"] for c in snapshot["channels"]] == [False, False]  # nothing was stored


# -- the four event sources ----------------------------------------------------------------------


async def test_notify_follows_the_settings_and_explicit_channels_win(world: JobsWorld) -> None:
    stack = stack_of(world)
    await stack.notifications.preferences.update(
        world.user, channels={"push": False, "telegram": True}
    )
    await stack.service.notify(user_id=world.user, text="Раз")  # settings: telegram only
    await stack.service.notify(user_id=world.user, text="Два", channels=["push"])  # explicit
    assert await deliveries(world) == [("Два", "push", "pending"), ("Раз", "telegram", "pending")]
    assert await inbox_titles(world) == ["Раз", "Два"]  # the inbox always gets the item


async def test_a_timer_goes_where_the_table_says(world: JobsWorld) -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    world.clock.set(now)
    stack = stack_of(world)
    await _plugin_state(world, "builtin.timer", [_item("t1", "Паста", now - timedelta(seconds=3))])
    await stack.notifications.preferences.update(
        world.user,
        channels={"telegram": True},
        matrix={"timer_alarm": {"push": False}, "job_result": {"telegram": False}},
    )
    assert await world.worker(stack).tick_deadlines() == 1
    assert await deliveries(world) == [("Таймер: Паста", "telegram", "pending")]


async def _reminder(world: JobsWorld, stack, channels):  # type: ignore[no-untyped-def]
    job, _ = await stack.store.create_job(
        NewJob(
            user_id=world.user,
            goal="Выпить таблетку",
            origin_type="user",
            origin_id="u",
            budget=stack.service.default_budget("reminder"),
            channels=channels,
            kind="reminder",
        )
    )
    worker = world.worker(stack)
    await worker.tick_jobs()
    await worker.wait_idle()
    return job


async def test_a_reminder_job_is_a_timer_and_a_task_result_is_a_job_result(
    world: JobsWorld,
) -> None:
    stack = stack_of(world)
    await stack.notifications.preferences.update(
        world.user,
        channels={"telegram": True},
        matrix={"timer_alarm": {"telegram": False}, "job_result": {"push": False}},
    )
    await _reminder(world, stack, None)
    assert await deliveries(world) == [("Напоминание: Выпить таблетку", "push", "pending")]

    job, _ = await stack.service.create_job(
        user_id=world.user, request={"goal": "Сводка"}, origin_type="user", origin_id="u"
    )
    assert job.delivery == {"channels": ["inbox", "push"], "auto": True}
    wire = job.to_wire()
    assert wire["delivery"] == {"channels": ["inbox", "push"]}
    assert_mvp_valid(wire, "job")


async def test_explicit_job_channels_win_over_the_settings(world: JobsWorld) -> None:
    stack = stack_of(world)
    await stack.notifications.preferences.update(world.user, channels={"push": False})
    job, _ = await stack.service.create_job(
        user_id=world.user,
        request={"goal": "g", "delivery": {"channels": ["telegram", "inbox"]}},
        origin_type="user",
        origin_id="u",
    )
    assert job.delivery == {"channels": ["telegram", "inbox"]}
    await _reminder(world, stack, ("push",))  # explicit push although push is switched off
    # The scripted model has no answer, so the first job fails; its notice still goes to the
    # telegram channel it asked for, although telegram is off in the settings.
    assert await deliveries(world) == [
        ("Напоминание: Выпить таблетку", "push", "pending"),
        ("Не выполнено: g", "telegram", "pending"),
    ]


async def test_schedule_without_channels_follows_the_settings_when_it_fires(
    world: JobsWorld,
) -> None:
    world.clock.set(datetime(2026, 10, 9, 12, 0, 5, tzinfo=UTC))
    stack = stack_of(world)
    base = {"rule": "RRULE:FREQ=HOURLY", "timezone": "UTC", "goal": "Размяться", "kind": "reminder"}
    follows, _ = await stack.service.create_schedule(user_id=world.user, request=base)
    explicit, _ = await stack.service.create_schedule(
        user_id=world.user,
        request={**base, "goal": "Выпить воды", "delivery": {"channels": ["inbox"]}},
    )
    assert follows.delivery == {"channels": ["inbox", "push"], "auto": True}
    assert explicit.delivery == {"channels": ["inbox"]}
    assert_mvp_valid(follows.to_wire(), "job", "Schedule")
    await stack.notifications.preferences.update(
        world.user, channels={"telegram": True, "push": False}
    )
    await stack.service.run_schedule_now(user_id=world.user, schedule_id=follows.id)
    await stack.service.run_schedule_now(user_id=world.user, schedule_id=explicit.id)
    worker = world.worker(stack)
    await worker.tick_jobs()
    await worker.wait_idle()
    # «Выпить воды» asked for the inbox only: no delivery rows at all.
    assert await deliveries(world) == [("Напоминание: Размяться", "telegram", "pending")]
    assert sorted(await inbox_titles(world)) == [
        "Напоминание: Выпить воды",
        "Напоминание: Размяться",
    ]


async def test_a_background_run_waiting_for_approval_is_an_approval_event(
    world: JobsWorld,
) -> None:
    stack = stack_of(world)
    await stack.notifications.preferences.update(
        world.user, channels={"telegram": True}, matrix={"approval": {"push": False}}
    )
    job, _ = await stack.service.create_job(
        user_id=world.user,
        request={"goal": "Закажи пиццу", "delivery": {"channels": ["inbox"]}},
        origin_type="user",
        origin_id="u",
    )
    await stack.store.notify_waiting_approval(job)
    # The job's own channels are about its result; this notice follows the settings.
    assert await deliveries(world) == [("Нужно подтверждение", "telegram", "pending")]
    async with world.factory() as session:
        item = await session.scalar(select(InboxItemRow))
    assert item is not None and "Закажи пиццу" in item.response["speech"]
    assert (item.source_type, item.source_id) == ("job", str(job.id))


# -- delivery: unavailable channels are skipped with a reason ----------------------------------


async def test_a_channel_that_cannot_reach_the_user_is_skipped_with_a_reason(
    world: JobsWorld,
) -> None:
    stack = stack_of(world, push=False)
    await stack.notifications.preferences.update(world.user, channels={"telegram": True})
    await stack.service.notify(user_id=world.user, text="Раз")
    await stack.notifications.dispatcher.run_once()
    async with world.factory() as session:
        rows = {
            row.channel: (row.status, row.last_error)
            for row in (await session.scalars(select(DeliveryRow))).all()
        }
    assert rows == {
        "push": ("skipped", "push_not_configured"),
        "telegram": ("skipped", "channel_not_connected"),
    }


async def test_plugin_channels_register_and_report_their_status(world: JobsWorld) -> None:
    stack = stack_of(world)
    board = FakeChannel("notify-board", label="Доска уведомлений")
    stack.notifications.channels.register(board)
    with pytest.raises(ValueError, match="channel name"):
        stack.notifications.channels.register(FakeChannel("inbox"))
    with pytest.raises(ValueError, match="channel name"):
        stack.notifications.channels.register(FakeChannel("Bad Name"))
    await stack.notifications.preferences.update(world.user, channels={"notify-board": True})
    snapshot = await stack.notifications.preferences.snapshot(world.user)
    assert_mvp_valid(snapshot, "notifications", "NotificationSettings")
    entry = next(c for c in snapshot["channels"] if c["name"] == "notify-board")
    assert (entry["label"], entry["status"], entry["enabled"]) == (
        "Доска уведомлений",
        "connected",
        True,
    )
    item = await stack.service.notify(user_id=world.user, text="Привет")
    await stack.notifications.dispatcher.run_once()
    [message] = board.messages
    assert (message.inbox_item_id, message.channel) == (item.id, "notify-board")


async def test_push_status_explains_why_it_is_not_connected(world: JobsWorld) -> None:
    from tests.jobs.conftest import create_device

    unconfigured = stack_of(world, push=False).notifications.preferences
    [push, _] = (await unconfigured.snapshot(world.user))["channels"]
    assert (push["status"], push["detail"]) == ("not_connected", "push_not_configured")

    stack = stack_of(world)
    [push, _] = (await stack.notifications.preferences.snapshot(world.user))["channels"]
    assert (push["status"], push["detail"]) == ("not_connected", "no_push_token")
    device = await create_device(world.factory, world.user)
    await stack.notifications.push_tokens.put(
        user_id=world.user, device_id=device, provider="fcm", token="t"
    )
    [push, _] = (await stack.notifications.preferences.snapshot(world.user))["channels"]
    assert (push["status"], push["detail"]) == ("connected", None)
