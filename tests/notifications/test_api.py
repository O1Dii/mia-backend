"""``/v1/notifications/settings`` on Postgres with real auth (bearer and web cookie)."""

from __future__ import annotations

from tests.fakes import assert_mvp_valid
from tests.jobs.conftest import requires_postgres
from tests.jobs.test_api import api
from tests.workspace.harness import android_login, key, web_headers, web_login

pytestmark = requires_postgres

URL = "/v1/notifications/settings"


async def test_settings_api_defaults_updates_and_isolation() -> None:
    async with api() as ctx, ctx.harness.client() as client, ctx.harness.client() as other:
        assert (await client.get(URL)).status_code == 401
        auth = await android_login(client, "owner")
        stranger = await android_login(other, "stranger")

        settings = (await client.get(URL, headers=auth)).json()
        assert_mvp_valid(settings, "notifications", "NotificationSettings")
        assert [(c["name"], c["enabled"], c["decided"]) for c in settings["channels"]] == [
            ("push", True, False),
            ("telegram", False, False),
        ]
        assert [e["id"] for e in settings["event_types"]] == [
            "timer_alarm",
            "job_result",
            "approval",
            "plugin_message",
        ]
        assert settings["matrix"]["job_result"] == {"push": True, "telegram": True}

        body = {
            "channels": {"telegram": True},
            "matrix": {"job_result": {"telegram": False}},
        }
        assert_mvp_valid(body, "notifications", "UpdateNotificationSettingsRequest")
        changed = await client.patch(URL, headers={**auth, **key()}, json=body)
        assert changed.status_code == 200, changed.text
        assert_mvp_valid(changed.json(), "notifications", "NotificationSettings")
        assert changed.json() == (await client.get(URL, headers=auth)).json()
        telegram = next(c for c in changed.json()["channels"] if c["name"] == "telegram")
        assert (telegram["enabled"], telegram["decided"]) == (True, True)
        assert changed.json()["matrix"]["job_result"] == {"push": True, "telegram": False}
        assert changed.json()["matrix"]["timer_alarm"] == {"push": True, "telegram": True}

        # A merge: a second request keeps what the first one set.
        again = await client.patch(
            URL, headers={**auth, **key()}, json={"matrix": {"approval": {"push": False}}}
        )
        assert again.json()["matrix"]["approval"] == {"push": False, "telegram": True}
        assert again.json()["matrix"]["job_result"]["telegram"] is False

        # The other user is untouched.
        assert (await other.get(URL, headers=stranger)).json() == settings


async def test_settings_api_rejects_unknown_names_and_bad_bodies() -> None:
    async with api() as ctx, ctx.harness.client() as client:
        auth = await android_login(client, "owner")
        channel = await client.patch(
            URL, headers={**auth, **key()}, json={"channels": {"fax": True}}
        )
        assert channel.status_code == 422
        assert channel.json()["error"]["code"] == "notification_channel_unknown"
        kind = await client.patch(
            URL, headers={**auth, **key()}, json={"matrix": {"weather": {"push": True}}}
        )
        assert kind.status_code == 422
        assert kind.json()["error"]["code"] == "notification_event_type_unknown"
        inbox = await client.patch(
            URL, headers={**auth, **key()}, json={"channels": {"inbox": False}}
        )
        assert inbox.status_code == 422
        for bad in ({"channels": {"push": "yes"}}, {"secret": 1}):
            response = await client.patch(URL, headers={**auth, **key()}, json=bad)
            assert response.status_code == 422 and "secret" not in response.text
        no_key = await client.patch(URL, headers=auth, json={"channels": {"push": False}})
        assert no_key.status_code == 422
        unchanged = (await client.get(URL, headers=auth)).json()
        assert all(not c["decided"] for c in unchanged["channels"])


async def test_the_web_session_uses_the_same_settings() -> None:
    async with api() as ctx, ctx.harness.client() as client:
        await web_login(client, "web-owner")
        changed = await client.patch(URL, headers=web_headers(), json={"channels": {"push": False}})
        assert changed.status_code == 200, changed.text
        assert (await client.get(URL)).json()["channels"][0]["enabled"] is False
        # Mutations must be same-origin.
        cross_site = await client.patch(
            URL,
            headers={**key(), "Origin": "https://evil.test", "Sec-Fetch-Site": "cross-site"},
            json={"channels": {"push": True}},
        )
        assert cross_site.status_code == 403


async def test_a_schedule_can_be_created_without_delivery() -> None:
    async with api() as ctx, ctx.harness.client() as client:
        auth = await android_login(client, "owner")
        created = await client.post(
            "/v1/schedules",
            headers={**auth, **key()},
            json={"rule": "RRULE:FREQ=DAILY;BYHOUR=8;BYMINUTE=30", "timezone": "UTC", "goal": "g"},
        )
        assert created.status_code == 201, created.text
        assert_mvp_valid(created.json(), "job", "Schedule")
        assert created.json()["delivery"] == {"channels": ["inbox", "push"]}
