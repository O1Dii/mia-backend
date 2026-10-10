"""Scenario 6: MindWeaver by voice (Mia side, production composition).

A MindWeaver-like remote plugin (``server.auth: mia_token``, ``clientId`` of the plugin's own
app) is published, installed and connected without a consent page through the user's grant of
that app; «Мия, сделай карту по этому тексту…» by voice starts a background job, the worker
runs it, the job calls the plugin with a 5-minute token Mia minted for it, and the result
lands in the inbox. Revoking the app in «Подключённые приложения» (``DELETE /v1/apps/{id}``)
takes the plugin's tools away until it is connected again.

The real MindWeaver process (map generation through Mia's ``/v1/chat/completions``, the tile,
«Войти через Mia», add-by-URL with Mia as the AS) is the cross-repo E2E
``mind-map-generator/backend/e2e_mia/test_mia_chain.py``; the web «Подключить» path is
``web/tests/journey.spec.ts`` («a mia_token plugin connects without a consent page»).
Live checks: UC-M51-1/2, UC-M51R1-1/2 (deployed MindWeaver, OI-M51-004).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from app.modules.oauth.models import OAuthGrant
from app.modules.plugins.catalog.package import load_package

from tests.catalog.remote_world import MCP_HOST, RESOURCE
from tests.e2e_mvp.harness import (
    Mvp,
    call,
    final,
    mvp_world,
    payloads,
    requires_stack,
    say,
    types,
)

pytestmark = requires_stack

PLUGIN = "team.mindweaver"
APP_CLIENT = f"https://{MCP_HOST}/oauth/client.json"
TEXT = "Фотосинтез — процесс, в котором растения превращают свет в энергию глюкозы."


@pytest.fixture
async def mw(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Mvp]:
    # The test tracker server (list_issues/create_issue) stands in for MindWeaver's /mcp; like a
    # real plugin it accepts whatever bearer Mia sends and checks it against Mia's AS itself.
    async with mvp_world(monkeypatch, linear=False) as world:
        world.remote.require_auth = False
        yield world


def package(tmp_path: Path) -> Path:
    manifest: dict[str, Any] = {
        "schemaVersion": "mia.plugin/1",
        "id": PLUGIN,
        "version": "1.0.0",
        "name": "MindWeaver",
        "listing": {"description": "Ментальные карты по тексту", "category": "work"},
        "server": {"kind": "remote", "url": RESOURCE, "auth": "mia_token", "clientId": APP_CLIENT},
        "skills": ["SKILL.md"],
        "tools": {
            "list_issues": {"risk": "read", "voice": True},
            "create_issue": {"risk": "write_reversible"},
        },
        "permissions": {
            "network": [MCP_HOST],
            "llm": {"viaHost": False},
            "memory": [],
            "notify": False,
        },
    }
    target = tmp_path / "mindweaver"
    target.mkdir()
    (target / "mia.json").write_text(json.dumps(manifest), "utf-8")
    (target / "SKILL.md").write_text("---\ndescription: Maps\n---\nBuild maps.\n", "utf-8")
    return target


async def signed_in_to_the_app(mvp: Mvp, user_id: str) -> uuid.UUID:
    """What «Войти через Mia» in the plugin's app leaves behind (real flow: the MW E2E)."""
    grant = uuid.uuid4()
    async with mvp.factory() as session, session.begin():
        session.add(
            OAuthGrant(
                id=grant,
                user_id=uuid.UUID(user_id),
                client_id=APP_CLIENT,
                client_name="MindWeaver",
                scopes="openid email llm.generate",
                monthly_limit_usd=5,
            )
        )
    return grant


def last_bearer(mvp: Mvp) -> str:
    header = mvp.remote.mcp_requests[-1].get("authorization", "")
    assert header.startswith("Bearer "), header
    return header.removeprefix("Bearer ")


async def test_mia_token_plugin_by_voice_through_a_job_and_revocation(
    mw: Mvp, tmp_path: Path
) -> None:
    mvp = mw
    published = await mvp.catalog.publish(
        load_package(package(tmp_path)), actor_user_id=mvp.admin_id, discover_token="discovery"
    )
    assert published.created and published.release.server_kind == "remote"

    phone = await mvp.phone("ann")
    await phone.register_device_tools()
    web = await mvp.browser("ann")
    installed = await mvp.install(phone, PLUGIN)
    assert installed["status"] == "needs_connection"
    iid = installed["id"]

    # «Подключить» before signing in to MindWeaver with Mia: the client is told to do that first.
    refused = await phone.post("/v1/connections", {"installation_id": iid})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "connection_sign_in_required"

    grant = await signed_in_to_the_app(mvp, phone.user_id)
    connected = await web.post("/v1/connections", {"installation_id": iid})
    assert connected.status_code == 200, connected.text
    assert connected.json()["status"] == "connected"
    assert "authorization_url" not in connected.json()  # no consent page
    assert (await phone.get(f"/v1/installations/{iid}")).json()["status"] == "active"

    # Voice: «сделай карту» → background job; the worker runs it and calls the plugin.
    goal = f"Построй ментальную карту по тексту: {TEXT}"
    mvp.model.then(
        call("background.start", {"goal": goal}),
        say("Запустила, скажу, когда карта будет готова."),
        call(f"{PLUGIN}.create_issue", {"title": "Фотосинтез"}),
        say(
            "Карта «Фотосинтез» готова.",
            {"kind": "text", "markdown": "Карта **Фотосинтез** готова."},
        ),
    )
    events = await phone.run(f"Мия, сделай карту по этому тексту: {TEXT}", profile="voice")
    [created] = payloads(events, "job.created")
    assert "готова" in final(events)["speech"]

    worker = mvp.worker()
    assert await worker.tick_jobs() == 1
    await worker.wait_idle()
    job = (await phone.get(f"/v1/jobs/{created['job_id']}")).json()
    assert job["status"] == "succeeded", job
    assert ("create_issue", {"title": "Фотосинтез"}) in mvp.remote.calls

    # The plugin got a short-lived Mia token for its own client, bound to its resource.
    token = last_bearer(mvp)
    access = await mvp.app.state.oauth.service.authenticate(token)
    assert access is not None
    assert (access.client_id, access.resource, str(access.user_id)) == (
        APP_CLIENT,
        RESOURCE,
        phone.user_id,
    )
    assert access.scopes == frozenset({"openid"})

    inbox = (await phone.get("/v1/inbox")).json()["items"]
    assert any("Фотосинтез" in json.dumps(item, ensure_ascii=False) for item in inbox), inbox

    # «Подключённые приложения» → «Отключить» MindWeaver: the minted token dies, the plugin
    # needs connecting again and its tools are no longer offered to the model; nothing more
    # reaches the plugin.
    revoked = await web.delete(f"/v1/apps/{grant}")
    assert revoked.status_code == 204, revoked.text
    assert await mvp.app.state.oauth.service.authenticate(token) is None
    calls_before = len(mvp.remote.calls)
    mvp.model.then(say("MindWeaver отключён, подключите его снова в магазине."))
    after = await phone.run("Покажи мои карты", profile="voice")
    assert types(after)[-1] == "run.completed"
    assert not any(name.startswith(f"{PLUGIN}.") for name in mvp.model.offered())
    # «Мои плагины» learns it from the health probe (M15-R1). The installation row stays
    # ``active`` and ``GET /v1/connections`` still says ``connected`` (stale label, OI in M60).
    health = (await phone.get(f"/v1/installations/{iid}/health")).json()
    assert (health["state"], health["tools"]) == ("needs_connection", 0), health
    assert len(mvp.remote.calls) == calls_before
