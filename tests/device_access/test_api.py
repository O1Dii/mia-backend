"""HTTP + Postgres (M70): device-only decisions, the phone's access API, run origins,
the WebView session bound to its device, push-delivered results, the SQL store."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.api.v1.approvals import build_approvals_router
from app.api.v1.device_access import build_device_access_router
from app.api.v1.device_tools import build_device_tools_router
from app.api.v1.web_handoff import install_web_handoff
from app.core.config import Settings
from app.core.ports.approval_service import ApprovalRequest
from app.core.ports.device_tool_channel import DeviceToolCall
from app.modules.auth.composition import request_authenticator
from app.modules.auth.models import WebSession
from app.modules.runs import NewRun, SqlRunStore
from app.modules.runs.domain import RunNotFoundError, RunRecord
from app.modules.tools.approvals import SqlApprovalService
from app.modules.tools.device import InMemoryDeviceToolChannel
from app.modules.tools.device_access import DeviceAccess
from app.modules.tools.device_origin import RunOriginResolver
from app.modules.tools.device_policy import CAPABILITIES
from app.modules.tools.device_store import (
    AccessLogEntry,
    GrantRecord,
    RunOrigin,
    SqlDeviceAccessStore,
)
from fastapi import Request
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from tests.auth.fakes import FakeRedis
from tests.device_access.helpers import DECLARED, FakePush
from tests.fakes import assert_mvp_valid
from tests.workspace.harness import (
    ORIGIN,
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


@dataclass
class Rig:
    harness: Harness
    store: SqlDeviceAccessStore
    access: DeviceAccess
    approvals: SqlApprovalService
    channel: InMemoryDeviceToolChannel
    push: FakePush
    runs: SqlRunStore


class OwnedRuns:
    """``RunService.get`` over the store: another user's run is not found."""

    def __init__(self, store: SqlRunStore) -> None:
        self._store = store

    async def get(self, *, user_id: uuid.UUID, run_id: uuid.UUID) -> RunRecord:
        record = await self._store.get(run_id, user_id=user_id)
        if record is None:
            raise RunNotFoundError("run not found")
        return record


@asynccontextmanager
async def rigged() -> AsyncIterator[Rig]:
    async with build_harness() as harness:
        settings = Settings(
            app_env="test",
            auth_jwt_secret=SecretStr(SECRET),
            auth_exchange_rate_limit=1000,
            auth_authenticated_rate_limit=5000,
        )
        install_web_handoff(
            harness.app, settings, session_factory=harness.factory, redis=FakeRedis()
        )
        store = SqlDeviceAccessStore(harness.factory)
        push = FakePush()
        access = DeviceAccess(store, notifier=push)
        approvals = SqlApprovalService(harness.factory)
        channel = InMemoryDeviceToolChannel(access=access)
        runs = SqlRunStore(harness.factory)
        authenticator = request_authenticator(harness.app)
        app = harness.app
        app.include_router(
            build_approvals_router(
                service=approvals, authenticator=authenticator, device_access=access
            )
        )
        app.include_router(
            build_device_access_router(access=access, calls=channel, authenticator=authenticator)
        )
        app.include_router(
            build_device_tools_router(
                channel=channel,
                runs=OwnedRuns(runs),
                authenticator=authenticator,
                sessions=store,
            )
        )
        resolver = RunOriginResolver(access, authenticator)

        @app.post("/test/prepare")
        async def prepare(request: Request) -> dict[str, Any]:
            body = await request.json()
            principal = await authenticator.authenticate(request)
            claimed = body.get("device_id")
            new = NewRun(
                user_id=uuid.UUID(principal.user_id),
                profile="chat",
                input_text="x",
                device_id=uuid.UUID(claimed) if claimed else None,
            )
            prepared = await resolver.prepare(request, new)
            origin = await access.origin(user_id=principal.user_id, run_id=str(new.id))
            return {
                "device_id": str(prepared.device_id) if prepared.device_id else None,
                "origin": None if origin is None else dataclasses.asdict(origin),
            }

        yield Rig(harness, store, access, approvals, channel, push, runs)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    async with rigged() as value:
        yield value


async def phone_login(
    client: AsyncClient, subject: str, device_id: str, name: str = "Galaxy S25"
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
                "app_version": "1.0",
                "device_name": name,
            },
        },
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def user_of(client: AsyncClient, headers: dict[str, str] | None = None) -> str:
    return str((await client.get("/v1/me", headers=headers or {})).json()["id"])


async def web_session_of(rig: Rig, user_id: str) -> str:
    async with rig.harness.factory() as session:
        found = await session.scalar(
            select(WebSession.id)
            .where(WebSession.user_id == uuid.UUID(user_id), WebSession.device_id.is_(None))
            .order_by(WebSession.created_at.desc())
        )
    return str(found)


async def browser_run(rig: Rig, user_id: str, web_session: str) -> str:
    record, _ = await rig.runs.create(
        NewRun(user_id=uuid.UUID(user_id), profile="chat", input_text="найди Яну")
    )
    origin = RunOrigin(
        str(record.id), user_id, "web", "браузер Chrome на macOS", web_session_id=web_session
    )
    rig.access.remember(origin)
    await rig.access.record_origin(origin)
    return str(record.id)


async def consent_approval(
    rig: Rig, user_id: str, run_id: str, phone: str, *, capability: str = "contacts.read"
) -> Any:
    scopes = ["run", "persistent"] if CAPABILITIES[capability].persistent_allowed else ["run"]
    return await rig.approvals.request(
        ApprovalRequest(
            user_id=user_id,
            run_id=run_id,
            tool_call_id="call_c1",
            tool="device.contacts.search",
            risk="read",
            summary="Подтвердите на телефоне: браузер просит поиск в контактах «Яна».",
            arguments={"query": "Яна"},
            device_consent={
                "waiting_for": "device",
                "target_device_id": phone,
                "target_device_name": "Galaxy S25",
                "requester": {"kind": "web", "label": "браузер Chrome на macOS"},
                "capability": capability,
                "capability_kind": CAPABILITIES[capability].kind,
                "capability_label": CAPABILITIES[capability].label,
                "model_sees": CAPABILITIES[capability].model_sees,
                "scopes": scopes,
            },
        )
    )


async def test_only_the_target_phone_decides_and_may_allow_permanently(rig: Rig) -> None:
    phone, tablet = str(uuid.uuid4()), str(uuid.uuid4())
    async with (
        rig.harness.client() as browser,
        rig.harness.client() as android,
        rig.harness.client() as other,
    ):
        await web_login(browser, "sub-consent")
        bearer = await phone_login(android, "sub-consent", phone)
        tablet_bearer = await phone_login(other, "sub-consent", tablet, "Tab")
        user = await user_of(android, bearer)
        session = await web_session_of(rig, user)
        run_id = await browser_run(rig, user, session)
        approval = await consent_approval(rig, user, run_id, phone)
        decision = {"decision": "approve", "args_hash": approval.args_hash, "scope": "persistent"}
        path = f"/v1/approvals/{approval.id}/decision"

        listed = (await browser.get("/v1/approvals")).json()["items"]
        assert listed[0]["device_consent"]["waiting_for"] == "device"
        assert_mvp_valid(listed[0], "approval")

        from_browser = await browser.post(path, headers=web_headers(), json=decision)
        assert from_browser.status_code == 403
        assert from_browser.json()["error"]["code"] == "approval_device_only"
        from_tablet = await other.post(path, headers={**tablet_bearer, **key()}, json=decision)
        assert from_tablet.status_code == 403

        decided = await android.post(path, headers={**bearer, **key()}, json=decision)
        assert decided.status_code == 200, decided.text
        body = decided.json()
        assert body["status"] == "approved" and body["decision"]["device_id"] == phone
        assert body["decision"]["channel"] == "android"
        assert_mvp_valid(body, "approval")

        grants = await android.get(f"/v1/devices/{phone}/grants", headers=bearer)
        assert grants.status_code == 200
        (grant,) = grants.json()["items"]
        assert_mvp_valid(grants.json(), "device-access", "DeviceGrantList")
        assert grant["capability"] == "contacts.read"
        assert grant["requester"] == {"kind": "web", "label": "браузер Chrome на macOS"}

        # External effects cannot be allowed permanently.
        telegram = await consent_approval(rig, user, run_id, phone, capability="telegram.draft")
        refused = await android.post(
            f"/v1/approvals/{telegram.id}/decision",
            headers={**bearer, **key()},
            json={"decision": "approve", "args_hash": telegram.args_hash, "scope": "persistent"},
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "approval_scope_unavailable"

        # The browser signs out: its permanent permission ends with the session.
        await browser.post("/v1/auth/web/logout", headers=web_headers())
        after = await android.get(f"/v1/devices/{phone}/grants", headers=bearer)
        assert after.json()["items"] == []


async def test_phone_manages_grants_capabilities_and_reads_its_log(rig: Rig) -> None:
    phone = str(uuid.uuid4())
    async with rig.harness.client() as android, rig.harness.client() as browser:
        bearer = await phone_login(android, "sub-settings", phone)
        await web_login(browser, "sub-settings")
        user = await user_of(android, bearer)
        now = datetime.now(UTC)
        grant = GrantRecord(
            id=str(uuid.uuid4()),
            user_id=user,
            target_device_id=phone,
            capability="alarm.clock",
            scope="persistent",
            requester_kind="device_app",
            requester_label="Mia на планшете",
            created_at=now,
            expires_at=now + timedelta(days=30),
            requester_key="device:x",
        )
        await rig.store.add_grant(grant)
        base = f"/v1/devices/{phone}"
        assert (await browser.get(f"{base}/grants")).status_code == 403
        other = str(uuid.uuid4())
        forbidden = await android.get(f"/v1/devices/{other}/grants", headers=bearer)
        assert forbidden.json()["error"]["code"] == "device_access_forbidden"

        revoked = await android.delete(f"{base}/grants/{grant.id}", headers={**bearer, **key()})
        assert revoked.status_code == 204
        again = await android.delete(f"{base}/grants/{grant.id}", headers={**bearer, **key()})
        assert (again.status_code, again.json()["error"]["code"]) == (404, "device_grant_not_found")

        settings = await android.get(f"{base}/capabilities", headers=bearer)
        assert_mvp_valid(settings.json(), "device-access", "DeviceCapabilitySettingList")
        assert {i["capability"]: i["remote"] for i in settings.json()["items"]} == {
            name: "ask" for name in CAPABILITIES
        }
        put = await android.put(
            f"{base}/capabilities/contacts.read",
            headers={**bearer, **key()},
            json={"remote": "deny"},
        )
        assert put.status_code == 200 and put.json()["remote"] == "deny"
        unknown = await android.put(
            f"{base}/capabilities/camera.photo", headers={**bearer, **key()}, json={"remote": "ask"}
        )
        assert unknown.json()["error"]["code"] == "device_capability_unknown"
        assert await rig.store.remote_settings(user_id=user, device_id=phone) == {
            "contacts.read": "deny"
        }

        for decision, expires in (("pending", now - timedelta(seconds=1)), ("allowed", None)):
            await rig.store.log(
                AccessLogEntry(
                    id=str(uuid.uuid4()),
                    user_id=user,
                    device_id=phone,
                    capability="contacts.read",
                    tool="device.contacts.search",
                    requester_kind="web",
                    requester_label="браузер Firefox на Windows",
                    decision=decision,
                    created_at=now,
                    expires_at=expires,
                )
            )
        log = await android.get(f"{base}/access-log", headers=bearer)
        assert_mvp_valid(log.json(), "device-access", "DeviceAccessLog")
        assert sorted(item["decision"] for item in log.json()["items"]) == ["allowed", "expired"]


async def test_push_delivered_call_is_answered_only_by_its_phone(rig: Rig) -> None:
    phone, tablet = str(uuid.uuid4()), str(uuid.uuid4())
    async with rig.harness.client() as android, rig.harness.client() as other:
        bearer = await phone_login(android, "sub-queue", phone)
        tablet_bearer = await phone_login(other, "sub-queue", tablet, "Tab")
        user = await user_of(android, bearer)
        put = await android.put(
            f"/v1/devices/{phone}/tools",
            headers={**bearer, **key()},
            json={
                "device_id": phone,
                "platform": "android",
                "app_version": "1.0",
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": dict(tool.input_schema),
                        "risk": tool.risk,
                        "voice": tool.voice,
                        "requires_unlocked": tool.requires_unlocked,
                        "timeout_ms": tool.timeout_ms,
                    }
                    for tool in DECLARED
                ],
            },
        )
        assert put.status_code == 204, put.text
        # Persisted: another client finds the phone's tools without the in-memory set.
        target = await rig.access.target(user)
        assert target is not None and target.device_id == phone and target.name == "Galaxy S25"

        record, _ = await rig.runs.create(
            NewRun(user_id=uuid.UUID(user), profile="chat", input_text="x")
        )
        published: list[Any] = []

        async def publish(**event: Any) -> None:
            published.append(event)

        rig.channel.bind_publisher(publish)
        call = DeviceToolCall(
            call_id="call_q1",
            run_id=str(record.id),
            device_id=phone,
            tool="device.contacts.search",
            arguments={"query": "Яна"},
            deadline_at=datetime.now(UTC) + timedelta(seconds=2),
            delivery="push",
        )
        invoked = asyncio.create_task(rig.channel.invoke(user_id=user, call=call))
        await asyncio.wait_for(rig.push.calls.get(), 5)
        queued = await android.get(f"/v1/devices/{phone}/tool-calls", headers=bearer)
        assert_mvp_valid(queued.json(), "device-access", "DeviceToolCallList")
        assert [item["call_id"] for item in queued.json()["items"]] == ["call_q1"]
        assert (await other.get(f"/v1/devices/{tablet}/tool-calls", headers=tablet_bearer)).json()[
            "items"
        ] == []

        result = {
            "call_id": "call_q1",
            "status": "succeeded",
            "content": "Found 1 contact: Яна +375 29 123-45-67",
            "structured_content": {"matches": [{"name": "Яна", "phone_count": 1}]},
        }
        url = f"/v1/runs/{record.id}/device-tool-results"
        wrong = await other.post(url, headers={**tablet_bearer, **key()}, json=result)
        assert wrong.status_code == 409
        right = await android.post(url, headers={**bearer, **key()}, json=result)
        assert right.status_code == 204
        outcome = await asyncio.wait_for(invoked, 5)
        assert outcome.status == "succeeded" and "+375" not in outcome.content


async def test_run_origin_uses_verified_devices_only(rig: Rig) -> None:
    phone, tablet = str(uuid.uuid4()), str(uuid.uuid4())
    async with (
        rig.harness.client() as android,
        rig.harness.client() as browser,
        rig.harness.client() as webview,
    ):
        bearer = await phone_login(android, "sub-origin", phone)
        await phone_login(android, "sub-origin-2", tablet, "Tab")  # another user's device
        await web_login(browser, "sub-origin")

        async def prepare(client: AsyncClient, headers: dict[str, str], claim: str | None) -> Any:
            response = await client.post(
                "/test/prepare", headers={**headers, **key()}, json={"device_id": claim}
            )
            assert response.status_code == 200, response.text
            return response.json()

        own = await prepare(android, bearer, phone)
        assert own["device_id"] == phone and own["origin"]["kind"] == "device_app"
        assert "Galaxy S25" in own["origin"]["label"]
        claimed_other = await prepare(android, bearer, tablet)
        assert claimed_other["device_id"] == phone
        no_claim = await prepare(android, bearer, None)
        assert no_claim["device_id"] is None and no_claim["origin"]["device_id"] == phone

        browser_headers = {**web_headers(), "User-Agent": CHROME}
        from_browser = await prepare(browser, browser_headers, phone)
        assert from_browser["device_id"] is None
        assert from_browser["origin"]["kind"] == "web"
        assert from_browser["origin"]["label"] == "браузер Chrome на macOS"

        issued = await android.post(
            "/v1/auth/web/handoff", headers={**bearer, **key()}, json={"path": "/chat"}
        )
        assert issued.status_code == 201
        redeemed = await webview.get(issued.json()["redeem_path"], follow_redirects=False)
        assert redeemed.status_code == 303
        user = await user_of(android, bearer)
        async with rig.harness.factory() as session:
            bound = await session.scalar(
                select(WebSession.device_id).where(
                    WebSession.user_id == uuid.UUID(user), WebSession.device_id.is_not(None)
                )
            )
        assert str(bound) == phone
        from_webview = await prepare(webview, {**web_headers(), "Origin": ORIGIN}, None)
        assert from_webview["device_id"] == phone
        assert from_webview["origin"]["kind"] == "device_webview"


async def test_sql_store_grants_follow_runs_sessions_and_time(rig: Rig) -> None:
    phone = str(uuid.uuid4())
    async with rig.harness.client() as android, rig.harness.client() as browser:
        bearer = await phone_login(android, "sub-store", phone)
        await web_login(browser, "sub-store")
        user = await user_of(android, bearer)
        session = await web_session_of(rig, user)
        run_id = await browser_run(rig, user, session)
        now = datetime.now(UTC)
        common = {
            "user_id": user,
            "target_device_id": phone,
            "capability": "contacts.read",
            "requester_kind": "web",
            "requester_label": "браузер",
            "created_at": now,
        }
        await rig.store.add_grant(
            GrantRecord(
                id=str(uuid.uuid4()),
                scope="run",
                run_id=run_id,
                expires_at=now + timedelta(hours=2),
                **common,  # type: ignore[arg-type]
            )
        )
        persistent = str(uuid.uuid4())
        await rig.store.add_grant(
            GrantRecord(
                id=persistent,
                scope="persistent",
                expires_at=now + timedelta(days=30),
                requester_key=f"web:{session}",
                requester_web_session_id=session,
                **common,  # type: ignore[arg-type]
            )
        )
        match = await rig.store.find_grants(
            user_id=user,
            target_device_id=phone,
            capability="contacts.read",
            run_id=run_id,
            requester_key=f"web:{session}",
            now=now,
        )
        assert match.run_grant_id is not None and match.persistent_grant_id == persistent
        other_run = await rig.store.find_grants(
            user_id=user,
            target_device_id=phone,
            capability="contacts.read",
            run_id=str(uuid.uuid4()),
            requester_key="web:someone-else",
            now=now,
        )
        assert other_run.run_grant_id is None and other_run.persistent_grant_id is None
        later = await rig.store.find_grants(
            user_id=user,
            target_device_id=phone,
            capability="contacts.read",
            run_id=run_id,
            requester_key=f"web:{session}",
            now=now + timedelta(days=31),
        )
        assert later.run_grant_id is None and later.persistent_grant_id is None
        origin = await rig.store.origin(user_id=user, run_id=run_id)
        assert origin is not None and origin.kind == "web" and origin.web_session_id == session
