"""MW3 gate: the seams between M15, M16, M17, M18 and M23 in one application.

Each session proves its own DoD in its suite (``tests/catalog``, ``tests/jobs``,
``tests/memory``, ``tests/feed``, ``tests/device_tools``). This module checks only
what the wave integration joined:

- the production app mounts every MW3 route next to each other (store, OAuth
  connections, jobs/schedules/inbox, push token, memory, feed, device tools);
- the shared core-tool registry carries M16 (``background.*``, ``schedule.*``,
  ``notify``) and M17 (``memory.*``, ``people.*``) tools at once;
- ``people.resolve`` (M17) understands the result the Android client (M23)
  actually sends for ``device.contacts.search``: ``{"matches": [{name, phone_count}]}``
  with names-only ``content`` (OI-M17-003).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.config import Settings
from app.core.ports.device_tool_channel import DeviceToolCall, DeviceToolResult
from app.core.ports.types import JsonObject
from app.main import create_app

from tests.fakes import text_step, tool_step
from tests.memory.conftest import FakeContactsChannel, MemoryWorld, mem  # noqa: F401
from tests.memory.test_e2e_runs_postgres import build_runs, completed, run
from tests.runs.conftest import requires_postgres
from tests.runs.gateways import answer, scripted

MW3_PATHS = {
    # M15
    "/v1/plugins",
    "/v1/plugins/{plugin_id}/install",
    # M16
    "/v1/jobs",
    "/v1/inbox",
    "/v1/devices/push-token",
    # M17
    "/v1/memory/people",
    # M18
    "/v1/feed",
    # M23
    "/v1/devices/{device_id}/tools",
    "/v1/runs/{run_id}/device-tool-results",
}


def test_production_app_mounts_every_mw3_surface() -> None:
    app = create_app(Settings(app_env="test"))
    paths = set(app.openapi()["paths"])
    missing = MW3_PATHS - paths
    assert not missing, missing
    assert any(p.startswith("/v1/connections") for p in paths)
    assert any(p.startswith("/v1/schedules") for p in paths)

    names = {tool.name for tool in app.state.tools.core_tools.tools()}
    assert {"background.start", "schedule.create", "notify"} <= names
    assert {"memory.remember", "people.resolve"} <= names


@dataclass
class AndroidContactsChannel(FakeContactsChannel):
    """Answers exactly like ``ContactsSearchTool.kt`` (M23): names and a phone count only."""

    async def invoke(self, *, user_id: str, call: DeviceToolCall) -> DeviceToolResult:
        self.calls.append(call)
        query = str(call.arguments.get("query", "")).lower()
        found = [c for c in self.contacts if query[:3] in str(c["display_name"]).lower()]
        names = [str(c["display_name"]) for c in found]
        structured: JsonObject = {
            "matches": [{"name": c["display_name"], "phone_count": len(c["phones"])} for c in found]
        }
        content = f"Found {len(found)} contact: {'; '.join(names)}"
        return DeviceToolResult(
            call_id=call.call_id,
            status="succeeded",
            content=content,
            structured_content=structured,
        )


@requires_postgres
async def test_people_resolve_reads_the_android_contacts_result(mem: MemoryWorld) -> None:  # noqa: F811
    channel = AndroidContactsChannel(
        contacts=[{"display_name": "Яна Гейтова", "phones": ["+375 29 111-22-33"]}]
    )
    router, tools = mem.router(channel)
    gateway = scripted(
        tool_step("call_1", "people.resolve", {"name": "Яна"}),
        text_step(answer("Нашла Яну Гейтову.")),
    )
    service = build_runs(mem, gateway, router)
    tools.bind_publisher(service.publish)
    events = await run(service, mem.user, "Найди Яну", device_id=uuid.uuid4())

    assert channel.calls and channel.calls[0].tool == "device.contacts.search"
    candidates = completed(events)[0]["structured_content"]["candidates"]
    assert candidates == [{"ref": "contact:1", "name": "Яна Гейтова", "source": "device_contacts"}]
