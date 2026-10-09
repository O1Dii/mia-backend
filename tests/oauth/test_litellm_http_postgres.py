"""LiteLLM over a real socket: only ``api_base`` and the Mia token change (Review §8.3, M51).

Mia runs under uvicorn on 127.0.0.1:8150 in its own thread and event loop; LiteLLM uses its
own HTTP stack (no injected client).
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn

from tests.oauth.harness import OAuthWorld, oauth_world
from tests.oauth.test_openai_compat_postgres import answer
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres
PORT = int(os.environ.get("MIA_M50_TEST_PORT", "8150"))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


@pytest.fixture
def served() -> Iterator[tuple[OAuthWorld, str]]:
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def run() -> None:
        async def main() -> None:
            async with oauth_world() as world:
                async with world.http() as http:
                    holder["token"] = (await world.login(http))["access_token"]
                server = uvicorn.Server(
                    uvicorn.Config(
                        world.app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="off"
                    )
                )
                holder.update(world=world, server=server)
                task = asyncio.create_task(server.serve())
                while not server.started and not task.done():  # noqa: ASYNC110
                    await asyncio.sleep(0.05)
                ready.set()
                await task

        try:
            asyncio.run(main())
        finally:
            ready.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(60) and "server" in holder, "uvicorn did not start"
    try:
        yield holder["world"], holder["token"]
    finally:
        holder["server"].should_exit = True
        thread.join(30)


def test_litellm_completion_and_stream_over_http(served: tuple[OAuthWorld, str]) -> None:
    import litellm

    world, token = served
    world.script(answer("Карта ", "готова"), answer("По", "ток"))
    base = f"http://127.0.0.1:{PORT}/v1"
    response: Any = litellm.completion(
        model="openai/mia:default",
        api_base=base,
        api_key=token,
        messages=[{"role": "user", "content": "Сделай карту"}],
    )
    assert response.choices[0].message.content == "Карта готова"
    stream: Any = litellm.completion(
        model="openai/mia:default",
        api_base=base,
        api_key=token,
        messages=[{"role": "user", "content": "Поток"}],
        stream=True,
    )
    assert "".join(chunk.choices[0].delta.content or "" for chunk in stream) == "Поток"
    with pytest.raises(litellm.exceptions.AuthenticationError):
        litellm.completion(
            model="openai/mia:default",
            api_base=base,
            api_key="mia_at_" + "x" * 43,
            messages=[{"role": "user", "content": "x"}],
            num_retries=0,
        )
