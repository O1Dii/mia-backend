"""SDKs of M50 end to end: ``mia-sdk`` (Python, in process) and ``@mia/sdk`` (JS, live socket).

Both are generated from ``contracts/mvp/v1/openapi.yaml``; the drift check fails when the
contract changed without regenerating the operation tables.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.oauth.harness import (
    CLIENT_ID,
    ISSUER,
    REDIRECT,
    OAuthWorld,
    oauth_world,
    served_world,
)
from tests.oauth.test_openai_compat_postgres import answer
from tests.runs.conftest import requires_postgres

ROOT = Path(__file__).resolve().parents[2]
SDK_PY = ROOT / "sdk/python"
SDK_JS = ROOT / "sdk/js"
PORT = int(os.environ.get("MIA_M50_TEST_PORT", "8150"))


def mia_sdk() -> Any:
    source = str(SDK_PY / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    return importlib.import_module("mia_sdk")


def test_generated_operation_tables_match_the_contract() -> None:
    result = subprocess.run(
        [sys.executable, str(SDK_PY / "scripts/generate_operations.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    operations = mia_sdk().OPERATIONS
    assert operations["createChatCompletion"]["scopes"] == ("llm.generate",)
    assert operations["createRun"]["idempotency_key"] is True


@pytest.fixture
async def world() -> AsyncIterator[OAuthWorld]:
    async with oauth_world() as built:
        yield built


@requires_postgres
async def test_python_sdk_sign_in_completions_refresh_and_revoke(world: OAuthWorld) -> None:
    sdk = mia_sdk()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app), base_url=ISSUER)
    oauth = sdk.MiaOAuth(issuer=ISSUER, client_id=CLIENT_ID, redirect_uri=REDIRECT, http=http)
    start = oauth.start(scope="openid email llm.generate")
    authorize = await http.get(start.url)
    location = authorize.headers["location"]
    handle = parse_qs(urlsplit(location).query)["request"][0]
    headers = {"x-test-user": str(world.user)}
    assert (await http.get(f"/v1/oauth/requests/{handle}", headers=headers)).status_code == 200
    decided = await http.post(
        f"/v1/oauth/requests/{handle}/decision",
        json={"decision": "approve"},
        headers={**headers, "Idempotency-Key": "python-sdk-e2e-key"},
    )
    redirect_to = decided.json()["redirect_to"]
    with pytest.raises(sdk.MiaOAuthError):
        await oauth.finish(redirect_to, state="forged", verifier=start.verifier)
    tokens = await oauth.finish(redirect_to, state=start.state, verifier=start.verifier)
    assert tokens.id_token and tokens.scope == "openid email llm.generate"
    assert (await oauth.userinfo(tokens.access_token))["email"].endswith("@example.test")

    world.script(answer("Один"), answer("Два", "!"), answer("Три"))
    mia = sdk.MiaClient(base_url=ISSUER, access_token=tokens.access_token, http=http)
    assert await mia.models() == ["mia:default"]
    first = await mia.chat_completions(
        {"model": "mia:default", "messages": [{"role": "user", "content": "1"}]}
    )
    assert first["choices"][0]["message"]["content"] == "Один"
    chunks = [
        c
        async for c in mia.stream_chat_completions(
            {"model": "mia:default", "messages": [{"role": "user", "content": "2"}]}
        )
    ]
    assert "".join(
        c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"]
    ) == ("Два!")
    third = await mia.responses({"model": "mia:default", "input": "3"})
    assert third["output"][0]["content"][0]["text"] == "Три"

    rotated = await oauth.refresh(tokens.refresh_token)
    with pytest.raises(sdk.MiaOAuthError):
        await oauth.refresh(tokens.refresh_token)  # reuse kills the family
    with pytest.raises(sdk.MiaOAuthError):
        await oauth.userinfo(rotated.access_token)
    await http.aclose()


@pytest.fixture
def served() -> Iterator[tuple[OAuthWorld, str]]:
    with served_world(PORT) as world_and_token:
        yield world_and_token


@requires_postgres
@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for @mia/sdk")
def test_js_sdk_end_to_end_against_live_mia(served: tuple[OAuthWorld, str]) -> None:
    world, _ = served
    world.script(answer("Привет из JS"), answer("По", "ток JS"))
    env = {
        **os.environ,
        "MIA_BASE_URL": f"http://127.0.0.1:{PORT}",
        "MIA_ISSUER": ISSUER,
        "MIA_TEST_USER": str(world.user),
        "MIA_CLIENT_ID": CLIENT_ID,
        "MIA_REDIRECT_URI": REDIRECT,
        "MIA_EXPECT_1": "Привет из JS",
        "MIA_EXPECT_2": "Поток JS",
    }
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--no-warnings", "tests/e2e.ts"],
        cwd=SDK_JS,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "E2E-OK" in result.stdout
    unit = subprocess.run(
        ["node", "--test", "--experimental-strip-types", "--no-warnings", "tests/unit.test.ts"],
        cwd=SDK_JS,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert unit.returncode == 0, unit.stdout + unit.stderr
