"""Shared steps of scenarios 4 and 5: Linear connected, Telegram linked, time travel."""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.modules.notifications.telegram import NOTIFY_YES_DATA
from app.modules.plugins.catalog.package import load_package

from tests.e2e_mvp.harness import WEBHOOK_SECRET, Mvp, Person

TELEGRAM_CHAT = 4242
TELEGRAM_SENDER = 777


async def linear_connected(mvp: Mvp, phone: Person, tmp_path: Path) -> str:
    """The store admin published Linear; the user installed it and connected the account."""
    published = await mvp.catalog.publish(
        load_package(mvp.linear_directory(tmp_path)),
        actor_user_id=mvp.admin_id,
        discover_token=mvp.auth_server.mint_token(),
    )
    assert published.created
    installed = await mvp.install(phone, "team.linear")
    callback = await mvp.connect(phone, installed["id"])
    assert callback.status_code in (200, 302, 303), callback.text
    state = (await phone.get(f"/v1/installations/{installed['id']}")).json()
    assert state["status"] == "active", state
    return str(installed["id"])


async def push_token(phone: Person, token: str) -> None:
    """What the app sends after FCM hands it a token (``PUT``; the harness helper POSTs)."""
    response = await phone.put(
        "/v1/devices/push-token", {"device_id": phone.device_id, "provider": "fcm", "token": token}
    )
    assert response.status_code == 204, response.text


async def webhook(mvp: Mvp, update: dict[str, Any]) -> None:
    response = await mvp.client.post(
        "/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        json=update,
    )
    assert response.status_code == 200, response.text


async def link_telegram(mvp: Mvp, phone: Person) -> None:
    """«Подключить Telegram-бота»: open the deep link, press Start, answer «Да»."""
    offer = await phone.post("/v1/telegram/link")
    assert offer.status_code == 201, offer.text
    query = urllib.parse.urlsplit(offer.json()["deep_link"]).query
    token = urllib.parse.parse_qs(query)["start"][0]
    chat = {"id": TELEGRAM_CHAT, "type": "private"}
    sender = {"id": TELEGRAM_SENDER, "is_bot": False}
    await webhook(
        mvp,
        {
            "update_id": 1,
            "message": {"message_id": 1, "chat": chat, "from": sender, "text": f"/start {token}"},
        },
    )
    await webhook(
        mvp,
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb-yes",
                "from": sender,
                "message": {"message_id": 55, "chat": chat},
                "data": NOTIFY_YES_DATA,
            },
        },
    )
    status = (await phone.get("/v1/telegram/link")).json()
    assert status["linked"] is True, status


class Moment:
    """A settable "now" for the worker side of a restarted process (the server's clock)."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def time_travel(application: Any, moment: Moment) -> None:
    """Point every clock of the jobs/notifications stack of ``application`` at ``moment``.

    Runs keep the real clock; only "when is something due" moves. This replaces waiting for
    a real Monday 09:00.
    """
    stack = application.state.jobs
    stack.store.clock = moment
    stack.scanner._clock = moment  # noqa: SLF001
    notifications = stack.notifications
    for part in (
        notifications.inbox,
        notifications.dispatcher,
        notifications.push_tokens,
        notifications.preferences,
    ):
        part._clock = moment  # noqa: SLF001


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)
