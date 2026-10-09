"""Boundaries of the chat-only adapters: errors, redirects, size and shape never leak or hang.

Replaces the provider-boundary tests of the removed web POC (``tests/web_poc/test_web_api.py``,
M31): upstream errors and redirects never echo the key, one request per call, a response over 1 MB
or not in the provider's format is a clean provider error.
"""

from __future__ import annotations

import httpx
import pytest
from app.modules.models.adapters import AdapterSettings
from app.modules.models.errors import ProviderError

from tests.models.helpers import collect, make_adapter, make_request

KEY = "sk-test-boundary-0123456789abcdef"
PROVIDERS = ("anthropic", "gemini", "ollama")


def quiet() -> AdapterSettings:
    async def no_sleep(_s: float) -> None:
        return None

    return AdapterSettings(sleep=no_sleep)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("code", [302, 400, 401, 403, 429, 500])
async def test_upstream_errors_and_redirects_never_echo_the_key(provider: str, code: int) -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {"location": "https://evil.example/steal"} if code == 302 else {}
        return httpx.Response(code, text=KEY, headers=headers)

    adapter, client = make_adapter(provider, httpx.MockTransport(upstream), quiet(), api_key=KEY)
    with pytest.raises(ProviderError) as failure:
        await collect(adapter, make_request())
    await client.aclose()
    assert KEY not in str(failure.value) and KEY not in repr(failure.value)
    assert {r.url.host for r in seen}.isdisjoint({"evil.example"})  # a redirect is never followed


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda _: httpx.Response(200, content=b"x" * 1_000_001), id="over-1mb"),
        pytest.param(lambda _: httpx.Response(200, text="not JSON"), id="not-json"),
        pytest.param(lambda _: httpx.Response(200, json={"choices": []}), id="foreign-shape"),
    ],
)
async def test_oversized_or_malformed_answers_are_clean_provider_errors(
    provider: str, answer: object
) -> None:
    transport = httpx.MockTransport(answer)  # type: ignore[arg-type]
    adapter, client = make_adapter(provider, transport, quiet(), api_key=KEY)
    with pytest.raises(ProviderError) as failure:
        await collect(adapter, make_request())
    await client.aclose()
    assert failure.value.code == "provider_unavailable"
    assert KEY not in str(failure.value)
