"""Credential-gated smoke of the real fallback provider (skips without DEEPINFRA_API_KEY).

MIA_SMOKE_TARGETS=tests/models/test_deepinfra_smoke.py sh ops/credential_smoke.sh
"""

from __future__ import annotations

import os

import httpx
import pytest
from app.modules.models import smoke
from app.modules.models.adapters import AdapterSettings, AdapterTarget, build_adapter
from app.modules.models.catalog import FALLBACK_MODEL, PROVIDERS
from pydantic import SecretStr

from tests.models.helpers import collect, make_request


async def test_real_glm_tool_loop_when_the_process_has_a_key() -> None:
    key = os.environ.get("DEEPINFRA_API_KEY")
    if not key:
        pytest.skip("DEEPINFRA_API_KEY is not configured in the process")
    spec = PROVIDERS["deepinfra"]
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        target = AdapterTarget(spec, FALLBACK_MODEL, spec.default_url, api_key=SecretStr(key))
        quick = await collect(
            build_adapter(client, target, AdapterSettings()), make_request(max_output_tokens=32)
        )
        assert quick
        result = await smoke.tool_scenario(client, target, "deepinfra")
    assert result["ok"], {k: v for k, v in result.items() if k != "provider"}
