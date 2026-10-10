"""HTTP API on Postgres with real auth: jobs, schedules, inbox, notify, push token."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from app.core.config import Settings
from app.modules.auth.models import Device
from app.modules.jobs.composition import JobsStack, install_jobs
from app.modules.notifications.models import PushTokenRow
from app.modules.tools.core_tools import CoreToolRegistry
from app.modules.users.models import User
from sqlalchemy import select

from tests.fakes import assert_mvp_valid
from tests.jobs.conftest import FakeFcm, _Runtime, jobs_settings, requires_postgres
from tests.runs.conftest import TEST_LIMITS
from tests.runs.gateways import scripted
from tests.workspace.harness import (
    Harness,
    android_login,
    build_harness,
    key,
    web_headers,
    web_login,
)

pytestmark = requires_postgres


@dataclass
class Api:
    harness: Harness
    jobs: JobsStack


@asynccontextmanager
async def api() -> AsyncIterator[Api]:
    from app.modules.runs import EngineConfig, RunEngine, RunNotifier, RunService, SqlRunStore

    async with build_harness() as harness:
        runs = RunService(
            RunEngine(
                store=SqlRunStore(harness.factory),
                notifier=RunNotifier(),
                gateway=scripted(),
                config=EngineConfig(limits=TEST_LIMITS),
            )
        )
        jobs = install_jobs(
            harness.app,
            Settings(app_env="test"),
            cast(Any, _Runtime(harness.factory)),
            runs=runs,
            core_tools=CoreToolRegistry(),
            jobs_settings=jobs_settings(jobs_max_active_per_user=3, notify_max_per_hour=2),
            fcm_client=FakeFcm().client(),
        )
        yield Api(harness, jobs)
        await runs.aclose()


async def _device_of(harness: Harness, subject: str) -> uuid.UUID:
    async with harness.factory() as session:
        device = await session.scalar(
            select(Device.id)
            .join(User, User.id == Device.user_id)
            .where(User.google_sub == subject)
        )
    assert device is not None
    return device


async def test_jobs_api_lifecycle_and_isolation() -> None:
    async with api() as ctx, ctx.harness.client() as client, ctx.harness.client() as other:
        auth = await android_login(client, "owner")
        stranger = await android_login(other, "stranger")
        assert (await client.get("/v1/jobs")).status_code == 401

        body = {"goal": "Собери сводку", "delivery": {"channels": ["push"]}}
        idem = key()
        created = await client.post("/v1/jobs", headers={**auth, **idem}, json=body)
        assert created.status_code == 202, created.text
        job = created.json()
        assert_mvp_valid(job, "job")
        assert job["origin"]["type"] == "user" and job["status"] == "queued"
        assert job["budget"] == {"max_steps": 40, "max_duration_seconds": 1800}
        replay = await client.post("/v1/jobs", headers={**auth, **idem}, json=body)
        assert replay.status_code == 202 and replay.json()["id"] == job["id"]
        conflict = await client.post("/v1/jobs", headers={**auth, **idem}, json={"goal": "x"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"

        assert (await client.get(f"/v1/jobs/{job['id']}", headers=auth)).json() == job
        foreign = await other.get(f"/v1/jobs/{job['id']}", headers=stranger)
        assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "job_not_found"
        assert (await client.get("/v1/jobs/not-a-uuid", headers=auth)).status_code == 404
        listed = (await client.get("/v1/jobs?limit=10", headers=auth)).json()
        assert [item["id"] for item in listed["items"]] == [job["id"]]
        assert listed["next_cursor"] is None
        bad_cursor = await client.get("/v1/jobs?cursor=zzz", headers=auth)
        assert bad_cursor.status_code == 400

        invalid = await client.post(
            "/v1/jobs", headers={**auth, **key()}, json={"goal": "", "secret": "x"}
        )
        assert invalid.status_code == 422 and "secret" not in invalid.text
        for _ in range(2):
            await client.post("/v1/jobs", headers={**auth, **key()}, json={"goal": "g"})
        limit = await client.post("/v1/jobs", headers={**auth, **key()}, json={"goal": "g"})
        assert limit.status_code == 409 and limit.json()["error"]["code"] == "job_limit_exceeded"

        assert (
            await other.post(f"/v1/jobs/{job['id']}/cancel", headers={**stranger, **key()})
        ).status_code == 404
        cancelled = await client.post(f"/v1/jobs/{job['id']}/cancel", headers={**auth, **key()})
        assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
        assert_mvp_valid(cancelled.json(), "job")


async def test_schedules_api() -> None:
    async with api() as ctx, ctx.harness.client() as client:
        auth = await android_login(client, "owner")
        body = {
            "rule": "RRULE:FREQ=DAILY;BYHOUR=8;BYMINUTE=30",
            "timezone": "Europe/Moscow",
            "goal": "Утренняя сводка",
            "delivery": {"channels": ["push", "telegram"]},
            "misfire_policy": "run_once",
        }
        created = await client.post("/v1/schedules", headers={**auth, **key()}, json=body)
        assert created.status_code == 201, created.text
        schedule = created.json()
        assert_mvp_valid(schedule, "job", "Schedule")
        assert schedule["misfire_policy"] == "run_once" and schedule["status"] == "active"
        bad = await client.post(
            "/v1/schedules", headers={**auth, **key()}, json={**body, "timezone": "Mars/Base"}
        )
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_timezone"
        often = await client.post(
            "/v1/schedules", headers={**auth, **key()}, json={**body, "rule": "RRULE:FREQ=MINUTELY"}
        )
        assert often.json()["error"]["code"] == "schedule_interval_too_short"

        path = f"/v1/schedules/{schedule['id']}"
        paused = await client.patch(path, headers={**auth, **key()}, json={"status": "paused"})
        assert paused.status_code == 200 and paused.json()["status"] == "paused"
        assert (await client.get("/v1/schedules", headers=auth)).json()["items"] == [paused.json()]
        run = await client.post(f"{path}/run", headers={**auth, **key()})
        assert run.status_code == 202 and run.json()["origin"] == {
            "type": "schedule",
            "id": schedule["id"],
        }
        deleted = await client.delete(path, headers={**auth, **key()})
        assert deleted.status_code == 204
        missing = await client.patch(path, headers={**auth, **key()}, json={"goal": "x"})
        assert (
            missing.status_code == 404 and missing.json()["error"]["code"] == "schedule_not_found"
        )


async def test_inbox_notify_and_push_token_with_web_and_android() -> None:
    async with api() as ctx, ctx.harness.client() as client, ctx.harness.client() as browser:
        auth = await android_login(client, "owner")
        await web_login(browser, "owner")
        device = await _device_of(ctx.harness, "owner")

        put = await client.put(
            "/v1/devices/push-token",
            headers={**auth, **key()},
            json={"device_id": str(device), "provider": "fcm", "token": "fcm-token-1"},
        )
        assert put.status_code == 204
        foreign = await client.put(
            "/v1/devices/push-token",
            headers={**auth, **key()},
            json={"device_id": str(uuid.uuid4()), "provider": "fcm", "token": "x"},
        )
        assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "device_not_found"
        async with ctx.harness.factory() as session:
            tokens = (await session.scalars(select(PushTokenRow.token))).all()
        assert tokens == ["fcm-token-1"]

        sent = await browser.post(
            "/v1/notify",
            headers=web_headers(),
            json={"text": "Привет из веба", "channels": ["push"]},
        )
        assert sent.status_code == 202, sent.text
        item = sent.json()
        assert_mvp_valid(item, "job", "InboxItem")

        inbox = (await client.get("/v1/inbox?unread=true", headers=auth)).json()
        assert [entry["id"] for entry in inbox["items"]] == [item["id"]]
        read = await client.post(f"/v1/inbox/{item['id']}/read", headers={**auth, **key()})
        assert read.status_code == 204
        assert (await client.get("/v1/inbox?unread=true", headers=auth)).json()["items"] == []
        everything = (await client.get("/v1/inbox", headers=auth)).json()["items"]
        assert everything[0]["read_at"] is not None
        missing = await client.post(f"/v1/inbox/{uuid.uuid4()}/read", headers={**auth, **key()})
        assert missing.status_code == 404
        # One item with its full text, for the web (cookie) and Android (bearer) (M74-R1).
        one = await browser.get(f"/v1/inbox/{item['id']}")
        assert one.status_code == 200 and one.json()["text"] == "Привет из веба"
        assert_mvp_valid(one.json(), "job", "InboxItem")
        android = await client.get(f"/v1/inbox/{item['id']}", headers=auth)
        assert android.json()["id"] == item["id"]
        for wrong in (str(uuid.uuid4()), "not-a-uuid"):
            gone = await client.get(f"/v1/inbox/{wrong}", headers=auth)
            assert gone.status_code == 404
            assert gone.json()["error"]["code"] == "inbox_item_not_found"

        await client.post("/v1/notify", headers={**auth, **key()}, json={"text": "2"})
        limited = await client.post("/v1/notify", headers={**auth, **key()}, json={"text": "3"})
        assert limited.status_code == 429
        assert limited.json()["error"] == {
            "code": "notify_rate_limited",
            "message": "Too many notifications in the last hour.",
            "request_id": limited.json()["error"]["request_id"],
            "retryable": True,
        }
        cross_site = await browser.post(
            "/v1/notify",
            headers={**key(), "Origin": "https://evil.test", "Sec-Fetch-Site": "cross-site"},
            json={"text": "x"},
        )
        assert cross_site.status_code == 403


async def test_routes_are_mounted_by_the_application() -> None:
    from app.main import create_app

    app = create_app(Settings(app_env="test"))
    paths = set(app.openapi()["paths"])
    assert {
        "/v1/jobs",
        "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/cancel",
        "/v1/schedules",
        "/v1/schedules/{schedule_id}",
        "/v1/schedules/{schedule_id}/run",
        "/v1/inbox",
        "/v1/inbox/{item_id}",
        "/v1/inbox/{item_id}/read",
        "/v1/notify",
        "/v1/devices/push-token",
    } <= paths
    assert isinstance(app.state.jobs, JobsStack)
    names = {tool.name for tool in app.state.tools.core_tools.tools()}
    assert {"background.start", "schedule.create", "notify"} <= names
    feed = app.state.runs.engine.feed  # chat titles (M74) wrap the away summary
    assert type(feed).__name__ == "TitlingFeed"
    assert type(feed.inner).__name__ == "AwaySummaryFeed"
