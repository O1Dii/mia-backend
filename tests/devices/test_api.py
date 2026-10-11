"""«Устройства» over HTTP + Postgres (M84): list, card, rename, disconnect, heartbeat, presence.

The DoD scenario: two phones and a browser of one account are listed with the right statuses;
revoking a persistent grant and disconnecting a device work; no phone numbers or content.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.api.v1.device_access import build_device_access_router
from app.api.v1.devices import build_devices_router
from app.api.v1.web_handoff import install_web_handoff
from app.core.config import Settings
from app.core.ports.device_tool_channel import DeviceToolDeclaration
from app.modules.auth.composition import request_authenticator
from app.modules.devices.composition import install_devices
from app.modules.devices.presence import ONLINE_WINDOW
from app.modules.devices.service import DeviceDirectory
from app.modules.notifications.channels import PushTokenStore
from app.modules.notifications.models import PushTokenRow
from app.modules.plugins.builtin import BUILTIN_ROOT
from app.modules.plugins.registry.manifest import PluginManifest, parse_manifest
from app.modules.tools.device import InMemoryDeviceToolChannel
from app.modules.tools.device_access import DeviceAccess
from app.modules.tools.device_policy import scrub_phone_numbers
from app.modules.tools.device_store import AccessLogEntry, GrantRecord, SqlDeviceAccessStore
from app.modules.tools.models import DeviceGrant, DeviceToolDeclarationSet
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select

from tests.auth.fakes import FakeRedis
from tests.fakes import assert_mvp_valid
from tests.workspace.harness import (
    SECRET,
    Harness,
    build_harness,
    key,
    requires_postgres,
    web_headers,
    web_login,
)

pytestmark = requires_postgres
CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)


def declaration(name: str) -> DeviceToolDeclaration:
    return DeviceToolDeclaration(
        name=name,
        description=f"{name} on the phone",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk="read",
        voice=True,
        requires_unlocked=False,
        timeout_ms=2000,
    )


PHONE_TOOLS = [
    declaration(name)
    for name in (
        "device.contacts.search",
        "device.alarm.set",
        "device.app.open",
        "telegram.handoff",
    )
]
REPORT = {
    "os": "Android 15",
    "model": "Samsung SM-S938B",
    "app_version": "0.9.0",
    "notifications": "granted",
    "capabilities": [
        {"capability": "contacts.read", "os_permission": "denied"},
        {"capability": "alarm.clock", "os_permission": "not_required"},
        {"capability": "apps.open", "os_permission": "not_required"},
        {"capability": "telegram.draft", "os_permission": "not_required"},
    ],
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True)
class FakePlugin:
    manifest: PluginManifest
    active: bool = True


class BuiltinPlugins:
    """The user's installed plugins: Будильник, Telegram, Заметки (and one disabled)."""

    def __init__(self) -> None:
        def load(name: str) -> PluginManifest:
            return parse_manifest(json.loads((BUILTIN_ROOT / name / "mia.json").read_text()))

        self.items = [
            FakePlugin(load("alarm")),
            FakePlugin(load("telegram")),
            FakePlugin(load("notes")),
            FakePlugin(load("news"), active=False),
        ]

    async def installed(self, *, user_id: str, workspace_id: str | None) -> Sequence[FakePlugin]:
        return self.items


@dataclass
class Rig:
    harness: Harness
    store: SqlDeviceAccessStore
    access: DeviceAccess
    directory: DeviceDirectory
    clock: Clock


@asynccontextmanager
async def rigged() -> AsyncIterator[Rig]:
    async with build_harness() as harness:
        app = harness.app
        settings = Settings(
            app_env="test",
            auth_jwt_secret=SecretStr(SECRET),
            auth_exchange_rate_limit=1000,
            auth_authenticated_rate_limit=5000,
        )
        install_web_handoff(app, settings, session_factory=harness.factory, redis=FakeRedis())
        authenticator = request_authenticator(app)
        store = SqlDeviceAccessStore(harness.factory)
        access = DeviceAccess(store)
        app.include_router(
            build_devices_router(
                tokens=PushTokenStore(harness.factory), authenticator=authenticator
            )
        )

        async def account_access(user_id: str, device_id: str) -> bool:
            return bool(await app.state.devices.owns_phone(user_id=user_id, device_id=device_id))

        app.include_router(
            build_device_access_router(
                access=access,
                calls=InMemoryDeviceToolChannel(access=access),
                authenticator=authenticator,
                account_access=account_access,
            )
        )
        clock = Clock()
        directory = install_devices(
            app, harness.factory, access=access, plugins=BuiltinPlugins(), push=True, clock=clock
        )
        yield Rig(harness, store, access, directory, clock)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    async with rigged() as value:
        yield value


async def phone_login(
    client: AsyncClient, subject: str, device_id: str, name: str
) -> dict[str, str]:
    nonce = await client.post("/v1/auth/google/nonce", headers=key())
    login = await client.post(
        "/v1/auth/google",
        headers=key(),
        json={
            "id_token": f"google:{subject}",
            "nonce": nonce.json()["nonce"],
            "device": {
                "id": device_id,
                "platform": "android",
                "app_version": "0.9.0",
                "device_name": name,
            },
        },
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def push_token(client: AsyncClient, bearer: dict[str, str], device_id: str) -> None:
    put = await client.put(
        "/v1/devices/push-token",
        headers={**bearer, **key()},
        json={"device_id": device_id, "provider": "fcm", "token": f"fcm-{device_id}"},
    )
    assert put.status_code == 204, put.text


def browser_headers() -> dict[str, str]:
    return {**web_headers(), "User-Agent": CHROME}


async def own_id(browser: AsyncClient) -> str:
    """The browser's own device id (its web session) from its heartbeat."""
    beat = await browser.post("/v1/devices/heartbeat", headers=browser_headers())
    return str(beat.json()["device_id"])


async def user_of(client: AsyncClient, headers: dict[str, str]) -> str:
    return str((await client.get("/v1/me", headers=headers)).json()["id"])


def by_kind(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in items}


async def test_two_phones_and_a_browser_are_listed_with_their_status(rig: Rig) -> None:
    galaxy, pixel = str(uuid.uuid4()), str(uuid.uuid4())
    async with (
        rig.harness.client() as browser,
        rig.harness.client() as phone_a,
        rig.harness.client() as phone_b,
    ):
        await web_login(browser, "sub-three")
        bearer_a = await phone_login(phone_a, "sub-three", galaxy, "Galaxy S25")
        bearer_b = await phone_login(phone_b, "sub-three", pixel, "Pixel 8")
        await push_token(phone_a, bearer_a, galaxy)
        beat = await phone_a.post(
            "/v1/devices/heartbeat", headers={**bearer_a, **key()}, json={"report": REPORT}
        )
        assert beat.status_code == 200, beat.text
        assert beat.json() == {"device_id": galaxy, "kind": "phone", "next_heartbeat_seconds": 60}
        assert_mvp_valid(beat.json(), "devices", "DeviceHeartbeat")
        user = await user_of(phone_a, bearer_a)
        await rig.store.save_declarations(
            user_id=user, device_id=galaxy, tools=PHONE_TOOLS, now=rig.clock.now
        )

        # Ten minutes later only the Galaxy and the browser are in contact.
        rig.clock.now += timedelta(minutes=10)
        await phone_a.post("/v1/devices/heartbeat", headers={**bearer_a, **key()})
        listed = await browser.get("/v1/devices", headers={"User-Agent": CHROME})
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert_mvp_valid(body, "devices", "DeviceList")
        items = body["items"]
        assert [item["kind"] for item in items] == ["browser", "phone", "phone"]
        browser_row, galaxy_row, pixel_row = items
        assert browser_row["current"] is True
        assert browser_row["name"] == "браузер Chrome на macOS"
        assert browser_row["presence"] == {"status": "online", "via": "contact"}
        assert browser_row["push"] == "not_applicable"
        assert galaxy_row["id"] == galaxy and galaxy_row["current"] is False
        assert galaxy_row["presence"] == {"status": "online", "via": "contact"}
        assert galaxy_row["push"] == "registered"
        assert galaxy_row["platform"] == {
            "os": "Android 15",
            "model": "Samsung SM-S938B",
            "app_version": "0.9.0",
            "client": None,
        }
        assert (galaxy_row["capabilities_ready"], galaxy_row["capabilities_total"]) == (3, 4)
        # The Pixel has no push token: it cannot be woken → «нет push».
        assert pixel_row["id"] == pixel
        assert pixel_row["presence"] == {"status": "no_push", "via": None}
        assert pixel_row["push"] == "missing"

        # With a token it reads «был(а) N мин назад»; an hour later «офлайн».
        await push_token(phone_b, bearer_b, pixel)
        rig.directory.tracker.forget("phone", pixel)  # registering the token was a contact
        async with rig.harness.factory() as session, session.begin():
            from app.modules.auth.models import Device  # noqa: PLC0415

            row = await session.get(Device, uuid.UUID(pixel))
            assert row is not None
            row.last_seen_at = rig.clock.now - timedelta(minutes=10)
        pixel_row = by_kind((await browser.get("/v1/devices")).json()["items"])["Pixel 8"]
        assert pixel_row["presence"]["status"] == "recent"
        assert pixel_row["last_seen_at"] is not None
        rig.clock.now += timedelta(minutes=55)
        await browser.post("/v1/devices/heartbeat", headers=browser_headers())
        rows = by_kind((await browser.get("/v1/devices")).json()["items"])
        assert rows["Pixel 8"]["presence"]["status"] == "offline"
        assert rows["Galaxy S25"]["presence"]["status"] == "recent"
        assert rows["браузер Chrome на macOS"]["presence"]["status"] == "online"

        # The cloud row: what works without devices (active plugins only).
        cloud = body["cloud"]
        assert cloud["label"] == "Облако Mia" and cloud["status"] == "online"
        assert [p["plugin_id"] for p in cloud["plugins"]] == [
            "builtin.alarm",
            "builtin.telegram",
            "builtin.notes",
        ]
        assert cloud["plugins"][2]["device"] is None
        # Seen from the phone: the Galaxy is «Это устройство» and comes first.
        from_phone = (await phone_a.get("/v1/devices", headers=bearer_a)).json()["items"]
        assert from_phone[0]["id"] == galaxy and from_phone[0]["current"] is True
        text = json.dumps(body, ensure_ascii=False)
        assert scrub_phone_numbers(text) == text  # no phone numbers anywhere


async def test_the_card_shows_capabilities_plugins_grants_and_the_log(rig: Rig) -> None:
    galaxy = str(uuid.uuid4())
    async with rig.harness.client() as browser, rig.harness.client() as phone:
        await web_login(browser, "sub-card")
        bearer = await phone_login(phone, "sub-card", galaxy, "Galaxy S25")
        await phone.post(
            "/v1/devices/heartbeat", headers={**bearer, **key()}, json={"report": REPORT}
        )
        user = await user_of(phone, bearer)
        await rig.store.save_declarations(
            user_id=user, device_id=galaxy, tools=PHONE_TOOLS[:3], now=rig.clock.now
        )
        await rig.store.set_remote(
            user_id=user, device_id=galaxy, capability="apps.open", remote="deny", now=rig.clock.now
        )
        grant = GrantRecord(
            id=str(uuid.uuid4()),
            user_id=user,
            target_device_id=galaxy,
            capability="alarm.clock",
            scope="persistent",
            requester_kind="web",
            requester_label="браузер Chrome на macOS",
            created_at=rig.clock.now,
            expires_at=rig.clock.now + timedelta(days=30),
            requester_key="web:x",
        )
        await rig.store.add_grant(grant)
        await rig.store.log(
            AccessLogEntry(
                id=str(uuid.uuid4()),
                user_id=user,
                device_id=galaxy,
                capability="contacts.read",
                tool="device.contacts.search",
                requester_kind="web",
                requester_label="браузер Chrome на macOS",
                decision="denied",
                created_at=rig.clock.now,
            )
        )

        card = await browser.get(f"/v1/devices/{galaxy}")
        assert card.status_code == 200, card.text
        body = card.json()
        assert_mvp_valid(body, "devices")
        caps = {row["capability"]: row for row in body["capabilities"]}
        assert list(caps) == ["contacts.read", "alarm.clock", "apps.open", "telegram.draft"]
        assert caps["contacts.read"]["enabled"] is True
        assert caps["contacts.read"]["os_permission"] == "denied"
        assert caps["contacts.read"]["ready"] is False
        assert caps["alarm.clock"]["ready"] is True
        assert [p["plugin_id"] for p in caps["alarm.clock"]["plugins"]] == ["builtin.alarm"]
        assert caps["apps.open"]["remote"] == "deny"
        assert caps["telegram.draft"]["enabled"] is False  # not declared by this app
        assert caps["telegram.draft"]["plugins"][0]["optional"] is False
        assert body["notifications"] == "granted"
        assert [g["id"] for g in body["grants"]] == [grant.id]
        assert body["links"]["access_log"] == f"/v1/devices/{galaxy}/access-log"
        assert body["actions"] == {"rename": True, "disconnect": True}

        # «Журнал доступа» and «Отозвать» from the browser (M84 opens them to the owner).
        log = await browser.get(body["links"]["access_log"])
        assert log.status_code == 200, log.text
        assert [e["decision"] for e in log.json()["items"]] == ["denied"]
        revoked = await browser.delete(
            f"/v1/devices/{galaxy}/grants/{grant.id}", headers=browser_headers()
        )
        assert revoked.status_code == 204
        assert (await browser.get(f"/v1/devices/{galaxy}")).json()["grants"] == []
        # The capability switches stay on the phone.
        put = await browser.put(
            f"/v1/devices/{galaxy}/capabilities/apps.open",
            headers=browser_headers(),
            json={"remote": "ask"},
        )
        assert put.json()["error"]["code"] == "device_access_forbidden"

        # The browser's own card: no capabilities, the permissions phones gave to it.
        session_id = await own_id(browser)
        given = GrantRecord(
            id=str(uuid.uuid4()),
            user_id=user,
            target_device_id=galaxy,
            capability="contacts.read",
            scope="persistent",
            requester_kind="web",
            requester_label="браузер Chrome на macOS",
            created_at=rig.clock.now,
            expires_at=rig.clock.now + timedelta(days=30),
            requester_key=f"web:{session_id}",
            requester_web_session_id=session_id,
        )
        await rig.store.add_grant(given)
        own = (await browser.get(f"/v1/devices/{session_id}")).json()
        assert_mvp_valid(own, "devices", "DeviceDetail")
        assert own["kind"] == "browser" and own["current"] is True
        assert own["capabilities"] == [] and own["links"]["access_log"] is None
        assert own["actions"]["disconnect"] is False
        assert [g["id"] for g in own["received_grants"]] == [given.id]
        assert own["received_grants"][0]["target_device_name"] == "Galaxy S25"


async def test_rename_is_used_everywhere_and_can_be_reset(rig: Rig) -> None:
    galaxy = str(uuid.uuid4())
    async with rig.harness.client() as browser, rig.harness.client() as phone:
        await web_login(browser, "sub-rename")
        bearer = await phone_login(phone, "sub-rename", galaxy, "Galaxy S25")
        user = await user_of(phone, bearer)
        renamed = await browser.patch(
            f"/v1/devices/{galaxy}", headers=browser_headers(), json={"name": " Мой\nGalaxy "}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "Мой Galaxy"
        assert renamed.json()["custom_name"] is True
        assert renamed.json()["default_name"] == "Galaxy S25"
        # Consent texts of ADR-0016 use the new name too.
        assert await rig.store.device_name(user_id=user, device_id=galaxy) == "Мой Galaxy"
        # A new sign-in on the phone keeps the user's name.
        await phone_login(phone, "sub-rename", galaxy, "Galaxy S25")
        listed = (await browser.get("/v1/devices")).json()["items"]
        assert by_kind(listed)["Мой Galaxy"]["id"] == galaxy
        too_long = await browser.patch(
            f"/v1/devices/{galaxy}", headers=browser_headers(), json={"name": "x" * 65}
        )
        assert too_long.status_code == 422
        reset = await browser.patch(
            f"/v1/devices/{galaxy}", headers=browser_headers(), json={"name": None}
        )
        assert reset.json()["name"] == "Galaxy S25" and reset.json()["custom_name"] is False
        session_id = await own_id(browser)
        own = await phone.patch(
            f"/v1/devices/{session_id}", headers={**bearer, **key()}, json={"name": "Рабочий ноут"}
        )
        assert own.json()["name"] == "Рабочий ноут" and own.json()["kind"] == "browser"


async def test_disconnecting_a_phone_signs_it_out_and_drops_push_and_grants(rig: Rig) -> None:
    galaxy = str(uuid.uuid4())
    async with rig.harness.client() as browser, rig.harness.client() as phone:
        await web_login(browser, "sub-off")
        bearer = await phone_login(phone, "sub-off", galaxy, "Galaxy S25")
        await push_token(phone, bearer, galaxy)
        user = await user_of(phone, bearer)
        await rig.store.save_declarations(
            user_id=user, device_id=galaxy, tools=PHONE_TOOLS, now=rig.clock.now
        )
        for target, requester_device in ((galaxy, None), (str(uuid.uuid4()), galaxy)):
            if requester_device is not None:
                await phone_login(phone, "sub-off", target, "Pixel 8")
            await rig.store.add_grant(
                GrantRecord(
                    id=str(uuid.uuid4()),
                    user_id=user,
                    target_device_id=target,
                    capability="alarm.clock",
                    scope="persistent",
                    requester_kind="device_app",
                    requester_label="Mia на телефоне",
                    created_at=rig.clock.now,
                    expires_at=rig.clock.now + timedelta(days=30),
                    requester_key=f"device:{requester_device or 'x'}",
                    requester_device_id=requester_device,
                )
            )
        # The phone's WebView is the phone: it ends with it.
        async with rig.harness.client() as webview:
            issued = await phone.post(
                "/v1/auth/web/handoff", headers={**bearer, **key()}, json={"path": "/devices"}
            )
            assert issued.status_code == 201, issued.text
            redeemed = await webview.get(issued.json()["redeem_path"], follow_redirects=False)
            assert redeemed.status_code == 303
            from_webview = (await webview.get("/v1/devices")).json()["items"]
            assert from_webview[0]["id"] == galaxy and from_webview[0]["current"] is True

            gone = await browser.post(f"/v1/devices/{galaxy}/disconnect", headers=browser_headers())
            assert gone.status_code == 204, gone.text
            assert (await phone.get("/v1/me", headers=bearer)).status_code == 401
            assert (await webview.get("/v1/devices")).status_code == 401

        async with rig.harness.factory() as session:
            tokens = await session.scalar(
                select(func.count())
                .select_from(PushTokenRow)
                .where(PushTokenRow.device_id == uuid.UUID(galaxy))
            )
            active_grants = await session.scalar(
                select(func.count())
                .select_from(DeviceGrant)
                .where(DeviceGrant.user_id == uuid.UUID(user), DeviceGrant.revoked_at.is_(None))
            )
            declared = await session.scalar(
                select(func.count())
                .select_from(DeviceToolDeclarationSet)
                .where(DeviceToolDeclarationSet.device_id == uuid.UUID(galaxy))
            )
        assert (tokens, active_grants, declared) == (0, 0, 0)
        assert await rig.access.target(user) is None
        listed = (await browser.get("/v1/devices")).json()["items"]
        assert galaxy not in [item["id"] for item in listed]
        again = await browser.post(f"/v1/devices/{galaxy}/disconnect", headers=browser_headers())
        assert (again.status_code, again.json()["error"]["code"]) == (404, "device_not_found")

        # Signing in again brings it back.
        await phone_login(phone, "sub-off", galaxy, "Galaxy S25")
        listed = (await browser.get("/v1/devices")).json()["items"]
        assert galaxy in [item["id"] for item in listed]


async def test_current_device_other_accounts_and_browser_disconnect(rig: Rig) -> None:
    galaxy, foreign = str(uuid.uuid4()), str(uuid.uuid4())
    async with (
        rig.harness.client() as browser,
        rig.harness.client() as phone,
        rig.harness.client() as stranger,
    ):
        await web_login(browser, "sub-mine")
        bearer = await phone_login(phone, "sub-mine", galaxy, "Galaxy S25")
        stranger_bearer = await phone_login(stranger, "sub-other", foreign, "Other")
        session_id = await own_id(browser)

        mine = await browser.post(f"/v1/devices/{session_id}/disconnect", headers=browser_headers())
        assert (mine.status_code, mine.json()["error"]["code"]) == (409, "device_is_current")
        own_phone = await phone.post(
            f"/v1/devices/{galaxy}/disconnect", headers={**bearer, **key()}
        )
        assert own_phone.status_code == 409

        for path, call in (
            (f"/v1/devices/{foreign}", browser.get),
            (f"/v1/devices/{str(uuid.uuid4())}", browser.get),
        ):
            missing = await call(path)
            assert (missing.status_code, missing.json()["error"]["code"]) == (
                404,
                "device_not_found",
            )
        renamed = await browser.patch(
            f"/v1/devices/{foreign}", headers=browser_headers(), json={"name": "mine"}
        )
        assert renamed.status_code == 404
        taken = await browser.post(f"/v1/devices/{foreign}/disconnect", headers=browser_headers())
        assert taken.status_code == 404
        grants = await browser.get(f"/v1/devices/{foreign}/grants")
        assert grants.json()["error"]["code"] == "device_access_forbidden"
        assert (await stranger.get("/v1/me", headers=stranger_bearer)).status_code == 200
        theirs = (await stranger.get("/v1/devices", headers=stranger_bearer)).json()["items"]
        assert [item["id"] for item in theirs] == [foreign]

        # The phone signs the browser out.
        out = await phone.post(f"/v1/devices/{session_id}/disconnect", headers={**bearer, **key()})
        assert out.status_code == 204
        assert (await browser.get("/v1/devices")).status_code == 401
        assert (await browser.get("/v1/me")).status_code == 401


async def test_heartbeat_reports_only_from_the_app_and_requests_count_as_contact(rig: Rig) -> None:
    galaxy = str(uuid.uuid4())
    async with rig.harness.client() as browser, rig.harness.client() as phone:
        await web_login(browser, "sub-beat")
        bearer = await phone_login(phone, "sub-beat", galaxy, "Galaxy S25")
        ignored = await browser.post(
            "/v1/devices/heartbeat", headers=browser_headers(), json={"report": REPORT}
        )
        assert ignored.status_code == 200 and ignored.json()["kind"] == "browser"
        card = (await browser.get(f"/v1/devices/{galaxy}")).json()
        assert card["reported_at"] is None and card["platform"]["os"] is None
        bad = await phone.post(
            "/v1/devices/heartbeat",
            headers={**bearer, **key()},
            json={"report": {"contacts": ["Яна"]}},
        )
        assert bad.status_code == 422

        # Any request is a contact; it is written at most once a minute.
        rig.clock.now += timedelta(hours=2)
        await phone.get("/v1/me", headers=bearer)
        async with rig.harness.factory() as session:
            from app.modules.auth.models import Device  # noqa: PLC0415

            row = await session.get(Device, uuid.UUID(galaxy))
            assert row is not None and row.last_seen_at == rig.clock.now
        rig.directory.tracker.forget("phone", galaxy)
        rig.clock.now += ONLINE_WINDOW + timedelta(seconds=1)
        status = by_kind((await browser.get("/v1/devices")).json()["items"])["Galaxy S25"]
        assert status["presence"]["status"] == "no_push"
    async with rig.harness.client() as anonymous:
        refused = await anonymous.post("/v1/devices/heartbeat", headers=key())
        assert refused.status_code == 401
        assert (await anonymous.get("/v1/devices")).status_code == 401
