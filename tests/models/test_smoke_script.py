"""The manual smoke script, exercised offline (fake auth world + replayed model API)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app.modules.models import smoke
from app.modules.models.adapters import AdapterSettings, AdapterTarget, build_adapter
from app.modules.models.catalog import PROVIDERS
from pydantic import SecretStr

from tests.models.auth_world import CLIENT_ID, AuthWorld
from tests.models.helpers import FakeTokens, collect, make_request
from tests.models.replay import ReplayTransport

KEY = "di-smoke-key-0123456789abcdef"


class Routed(httpx.AsyncBaseTransport):
    def __init__(self, world: AuthWorld, api: ReplayTransport) -> None:
        self._world = world
        self._api = api

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.openai.com":
            return await self._world.transport.handle_async_request(request)
        return await self._api.handle_async_request(request)


def browser(world: AuthWorld) -> Callable[[str], None]:
    def open_url(url: str) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        world.nonce = query["nonce"]
        httpx.get(
            query["redirect_uri"],
            params={"code": "c", "state": query["state"], "client_id": CLIENT_ID},
        )

    return open_url


async def test_gpt_plan_smoke_signs_in_runs_the_tool_loop_and_records_a_replayable_cassette(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = AuthWorld()
    api = ReplayTransport("plan_tool_loop")
    code = await smoke.run_smoke(
        ["gpt-plan"],
        transport=Routed(world, api),
        open_browser=browser(world),
        model="gpt-6.1-sol",
        port=0,
        record_dir=tmp_path,
        environ={},
    )
    out = capsys.readouterr().out
    assert code == 0 and "gpt-plan: PASS" in out
    assert world.revoked  # signed out at the end
    assert "access-1" not in out and "refresh-1" not in out

    path = tmp_path / "live_gpt_plan_tool_loop.json"
    recorded = json.loads(path.read_text())
    assert len(recorded["interactions"]) == 3
    assert "Bearer" not in json.dumps(recorded) and "access-1" not in json.dumps(recorded)

    # the recording replays through the same adapter stack
    spec = PROVIDERS["chatgpt-plan"]
    replay = ReplayTransport(path)
    client = httpx.AsyncClient(transport=replay)
    target = AdapterTarget(spec, "gpt-6.1-sol", spec.default_url, tokens=FakeTokens())
    result = await smoke.tool_scenario(client, target, "replayed")
    await client.aclose()
    assert result["ok"] and replay.remaining == 0


async def test_glm_smoke_reads_the_key_only_from_the_environment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = ReplayTransport("glm_tool_loop")
    code = await smoke.run_smoke(["glm"], transport=api, environ={"DEEPINFRA_API_KEY": KEY})
    out = capsys.readouterr().out
    assert code == 0 and "glm: PASS" in out and KEY not in out
    assert api.requests[0].headers["authorization"] == f"Bearer {KEY}"


async def test_glm_smoke_without_a_key_is_not_run_rather_than_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await smoke.run_smoke(["glm"], environ={})
    assert code == 0 and "NOT RUN" in capsys.readouterr().out


async def test_recorded_cassette_round_trips_through_the_replay_transport(tmp_path: Path) -> None:
    inner = ReplayTransport("glm_tool_loop")
    recorder = smoke.RecordingTransport(inner, {"api.deepinfra.com"})
    client = httpx.AsyncClient(transport=recorder)
    spec = PROVIDERS["deepinfra"]
    target = AdapterTarget(spec, "m", spec.default_url)

    target = AdapterTarget(spec, "m", spec.default_url, api_key=SecretStr(KEY))
    await collect(build_adapter(client, target, AdapterSettings()), make_request())
    await client.aclose()
    smoke.write_cassette(tmp_path, "rt", recorder)
    saved = json.loads((tmp_path / "rt.json").read_text())
    assert saved["interactions"][0]["response"]["events"][-1]["data"] == "[DONE]"
    assert KEY not in json.dumps(saved)


def test_scrub_redacts_tokens_emails_and_opaque_blobs() -> None:
    cleaned = smoke.scrub(
        {
            "a": "Bearer abc.def",
            "b": "me@example.com",
            "c": "x" * 120,
            "d": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
            "ok": "call_1",
        }
    )
    assert cleaned["ok"] == "call_1"
    assert all("abc" not in v and "@" not in v for k, v in cleaned.items() if k in "ab")
    assert cleaned["c"] == "[scrubbed]" and "eyJ" not in cleaned["d"]
