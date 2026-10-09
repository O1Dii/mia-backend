"""Delivery loop: retries with backoff, token invalidation, Telegram extension point."""

from __future__ import annotations

from app.modules.notifications.channels import DeliveryResult, OutboundMessage
from app.modules.notifications.models import DeliveryRow, PushTokenRow
from sqlalchemy import select

from tests.jobs.conftest import JobsWorld, create_device, requires_postgres
from tests.runs.gateways import scripted

pytestmark = requires_postgres


class FakeTelegram:
    name = "telegram"

    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def deliver(self, message: OutboundMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult("sent")


async def _rows(world: JobsWorld) -> dict[str, DeliveryRow]:
    async with world.factory() as session:
        return {row.channel: row for row in (await session.scalars(select(DeliveryRow))).all()}


async def test_push_retries_with_backoff_and_drops_unregistered_tokens(
    world: JobsWorld,
) -> None:
    stack = world.stack(world.run_service(scripted()))
    notifications = stack.notifications
    phone = await create_device(world.factory, world.user)
    tablet = await create_device(world.factory, world.user)
    await notifications.push_tokens.put(
        user_id=world.user, device_id=phone, provider="fcm", token="busy"
    )
    await notifications.push_tokens.put(
        user_id=world.user, device_id=tablet, provider="fcm", token="gone"
    )
    world.fcm.responses = {"busy": (503, {}), "gone": (404, {"error": {"status": "NOT_FOUND"}})}
    await stack.service.notify(user_id=world.user, text="Привет", channels=["push"])

    assert await notifications.dispatcher.run_once() == 1
    row = (await _rows(world))["push"]
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "push_unavailable")
    async with world.factory() as session:
        tokens = {
            t.token: t.invalidated_at for t in (await session.scalars(select(PushTokenRow))).all()
        }
    assert tokens["gone"] is not None and tokens["busy"] is None
    assert await notifications.dispatcher.run_once() == 0  # backoff: not due yet

    world.fcm.responses = {}
    world.clock.advance(20)
    assert await notifications.dispatcher.run_once() == 1
    row = (await _rows(world))["push"]
    assert (row.status, row.attempts) == ("sent", 2)
    assert [push["token"] for push in world.fcm.sent] == ["busy"]


async def test_telegram_channel_is_an_extension_point(world: JobsWorld) -> None:
    stack = world.stack(world.run_service(scripted()), push=False)
    notifications = stack.notifications
    await stack.service.notify(user_id=world.user, text="Раз", channels=["telegram"])
    await notifications.dispatcher.run_once()
    assert (await _rows(world))["telegram"].status == "skipped"

    telegram = FakeTelegram()
    notifications.channels.register(telegram)
    item = await stack.service.notify(user_id=world.user, text="Два", channels=["telegram", "push"])
    await notifications.dispatcher.run_once()
    [message] = telegram.messages
    assert (message.inbox_item_id, message.text, message.channel) == (item.id, "Два", "telegram")
    async with world.factory() as session:
        rows = (
            await session.scalars(select(DeliveryRow).where(DeliveryRow.inbox_item_id == item.id))
        ).all()
    assert {(r.channel, r.status, r.last_error) for r in rows} == {
        ("telegram", "sent", None),
        ("push", "skipped", "push_not_configured"),
    }


async def test_push_token_moves_between_devices_and_users(world: JobsWorld) -> None:
    stack = world.stack(world.run_service(scripted()))
    tokens = stack.notifications.push_tokens
    mine = await create_device(world.factory, world.user)
    theirs = await create_device(world.factory, world.stranger)
    await tokens.put(user_id=world.user, device_id=mine, provider="fcm", token="t1")
    await tokens.put(user_id=world.user, device_id=mine, provider="fcm", token="t2")
    assert [t for _, t in await tokens.active(world.user)] == ["t2"]
    # The phone is signed into another account and registers the same token there.
    await tokens.put(user_id=world.stranger, device_id=theirs, provider="fcm", token="t2")
    assert await tokens.active(world.user) == []
    assert [t for _, t in await tokens.active(world.stranger)] == ["t2"]
