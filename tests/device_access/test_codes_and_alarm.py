"""M70 error codes are registered; ``builtin.alarm`` never sets the system alarm itself."""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.plugins.builtin import InMemoryPluginStateStore
from app.modules.runs.errors import RUN_CODES
from app.modules.tools.errors import DEVICE_ACCESS_CODES, ROUTER_CODES

from tests.plugins.support import Caller, FakeClock, connect, make_services, meta, server_for
from tests.plugins.test_countdown_plugins import iso

REGISTRY = Path("contracts/mvp/v1/error-codes.json")
SKILL = Path("app/plugins/builtin/alarm/SKILL.md")


def test_device_access_codes_are_registered_and_unique() -> None:
    registry = {entry["code"]: entry for entry in json.loads(REGISTRY.read_text("utf-8"))}
    for code, known in DEVICE_ACCESS_CODES.items():
        entry = registry[code]
        assert entry["owner"] == "M70"
        assert (entry["retryable"], entry["public_message"], entry["http_status"]) == (
            known.retryable,
            known.message,
            known.http_status,
        )
    assert not set(DEVICE_ACCESS_CODES) & (set(ROUTER_CODES) | set(RUN_CODES))


async def test_first_cloud_alarm_offers_the_clock_once() -> None:
    clock = FakeClock()
    state = InMemoryPluginStateStore()
    async with connect(server_for("builtin.alarm", make_services(clock, state=state))) as client:
        call = Caller(client, meta())
        first = await call.text("set", {"at": iso(clock, 3600), "label": "подъём"})
        second = await call.text("set", {"at": iso(clock, 7200), "label": "второй"})
    assert "в «Часах»" in first and "сам не ставь" in first
    assert "Часах" not in second


def test_alarm_skill_forbids_implicit_device_alarms() -> None:
    text = SKILL.read_text("utf-8")
    assert "только если пользователь прямо попросил" in text
    assert "вызови и его" not in text


def test_application_wires_device_access() -> None:
    from app.core.config import Settings
    from app.main import create_app
    from app.modules.tools.device_origin import RunOriginResolver

    app = create_app(Settings(app_env="test"))
    assert isinstance(app.state.run_origins, RunOriginResolver)
    stack = app.state.tools
    assert stack.device_access is not None
    paths = set(app.openapi()["paths"])
    assert {
        "/v1/devices/{device_id}/tool-calls",
        "/v1/devices/{device_id}/grants",
        "/v1/devices/{device_id}/grants/{grant_id}",
        "/v1/devices/{device_id}/access-log",
        "/v1/devices/{device_id}/capabilities",
        "/v1/devices/{device_id}/capabilities/{capability}",
    } <= paths
