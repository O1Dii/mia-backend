"""LiteLLM over a real socket: only ``api_base`` and the Mia token change (Review §8.3, M51).

Mia runs under uvicorn on 127.0.0.1:8150 in its own thread and event loop; LiteLLM uses its
own HTTP stack (no injected client).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from tests.oauth.harness import OAuthWorld, served_world
from tests.oauth.test_openai_compat_postgres import answer
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres
PORT = int(os.environ.get("MIA_M50_TEST_PORT", "8150"))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


@pytest.fixture
def served() -> Iterator[tuple[OAuthWorld, str]]:
    with served_world(PORT) as world_and_token:
        yield world_and_token


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
