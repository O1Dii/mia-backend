"""Security boundaries and all provider protocols, with no live credentials or cloud calls."""

import json
import secrets
from collections.abc import Callable
from typing import cast

import httpx
import pytest
from app.web_poc.main import COOKIE, create_web_app
from app.web_poc.providers import PROVIDERS
from fastapi.testclient import TestClient

HEADERS = {"X-Mia-Web": "1", "Origin": "http://127.0.0.1:5173"}


def add(client: TestClient, provider: str = "openai", **overrides: object) -> httpx.Response:
    return cast(
        httpx.Response,
        client.post(
            "/api/web/connections",
            headers=HEADERS,
            json={
                "provider": provider,
                "label": "Test model",
                "model": "test-model",
                "api_key": secrets.token_urlsafe(20),
                **overrides,
            },
        ),
    )


def test_credentials_are_never_returned_and_sessions_are_isolated() -> None:
    application = create_web_app()
    with TestClient(application) as owner, TestClient(application) as other:
        key = secrets.token_urlsafe(30)
        connection = add(owner, api_key=key)
        assert connection.status_code == 201
        assert key not in connection.text
        assert "api_key" not in connection.json()
        assert connection.json()["has_key"] is True
        cookie = connection.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert f"{COOKIE}=" in cookie
        assert key not in owner.get("/api/web/connections").text
        assert other.get("/api/web/connections").json() == []
        connection_id = connection.json()["id"]
        assert (
            other.delete(f"/api/web/connections/{connection_id}", headers=HEADERS).status_code
            == 404
        )
        assert (
            owner.delete(f"/api/web/connections/{connection_id}", headers=HEADERS).status_code
            == 204
        )
        assert owner.get("/api/web/connections").json() == []


def test_validation_does_not_echo_secret_inputs() -> None:
    with TestClient(create_web_app()) as client:
        key = secrets.token_urlsafe(30)
        for invalid in [
            {"model": "invalid model"},
            {"provider": "unknown"},
            {"label": " "},
            {"api_key": key + "\nsecret"},
            {"unexpected": key},
        ]:
            response = (
                add(client, api_key=key, **invalid)
                if "api_key" not in invalid
                else add(client, **invalid)
            )
            assert response.status_code == 422
            assert key not in response.text
            assert "input" not in response.text


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "null", "http://localhost.evil.example"]
)
def test_external_sites_cannot_use_the_local_key_proxy(origin: str) -> None:
    with TestClient(create_web_app()) as client:
        response = client.post(
            "/api/web/connections", json={}, headers={"X-Mia-Web": "1", "Origin": origin}
        )
        assert response.status_code == 403


def test_localhost_and_request_boundaries() -> None:
    with TestClient(create_web_app()) as client:
        assert client.post("/api/web/connections", json={}).status_code == 403
        assert (
            client.get("/api/web/connections", headers={"Host": "evil.example"}).status_code == 400
        )
        assert (
            client.post("/api/web/connections", headers=HEADERS, content="text").status_code == 415
        )
        assert (
            client.post(
                "/api/web/connections",
                headers={**HEADERS, "Content-Type": "application/json"},
                content="x" * 150001,
            ).status_code
            == 413
        )
        assert client.get("/api/web/health").headers["cache-control"] == "no-store"
    with TestClient(create_web_app(), client=("192.168.1.50", 1234)) as remote:
        assert remote.get("/api/web/connections").status_code == 403


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1234/v1",
        "http://192.168.1.1:1234/v1",
        "http://127.0.0.1:80",
        "https://evil.example/v1",
        "http://127.0.0.1:1234/v1?key=value",
        "http://user:password@127.0.0.1:1234/v1",
    ],
)
def test_local_provider_cannot_become_an_arbitrary_network_proxy(url: str) -> None:
    with TestClient(create_web_app()) as client:
        assert add(client, "local", base_url=url, api_key="").status_code == 422


def test_local_models_need_no_key_and_cloud_endpoints_cannot_be_overridden() -> None:
    with TestClient(create_web_app()) as client:
        local = add(client, "local", base_url="http://127.0.0.1:1234/v1", api_key="")
        assert local.status_code == 201 and local.json()["has_key"] is False
        assert add(client, api_key="").status_code == 422
        assert add(client, base_url="https://evil.example").status_code == 422


@pytest.mark.parametrize("provider", list(PROVIDERS))
def test_every_adapter_sends_its_native_protocol_and_preserves_conversation(provider: str) -> None:
    key = secrets.token_urlsafe(30)

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert key not in str(request.url)
        assert body["model"] == "test-model" if provider != "gemini" else "model" not in body
        if provider == "openai":
            assert request.url.path == "/v1/responses"
            assert body["store"] is False
            assert body["input"][1] == {"role": "assistant", "content": "Earlier answer"}
            assert request.headers["authorization"] == f"Bearer {key}"
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Actual answer"}],
                        }
                    ]
                },
            )
        if provider == "anthropic":
            assert request.url.path == "/v1/messages"
            assert request.headers["x-api-key"] == key
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert body["messages"][1]["role"] == "assistant"
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "Actual answer"}]}
            )
        if provider == "gemini":
            assert request.url.path.endswith("/test-model:generateContent")
            assert request.headers["x-goog-api-key"] == key
            assert body["contents"][1] == {"role": "model", "parts": [{"text": "Earlier answer"}]}
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "hidden reasoning", "thought": True},
                                    {"text": "Actual answer"},
                                ]
                            }
                        }
                    ]
                },
            )
        if provider == "ollama":
            assert request.url.path == "/api/chat" and body["stream"] is False
            assert body["messages"][1]["role"] == "assistant"
            return httpx.Response(200, json={"message": {"content": "Actual answer"}})
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == f"Bearer {key}"
        assert body["messages"][1]["role"] == "assistant"
        return httpx.Response(200, json={"choices": [{"message": {"content": "Actual answer"}}]})

    with TestClient(create_web_app(transport=httpx.MockTransport(upstream))) as client:
        connection = add(client, provider, api_key=key).json()
        response = client.post(
            "/api/web/chat",
            headers=HEADERS,
            json={
                "connection_id": connection["id"],
                "messages": [
                    {"role": "user", "content": "Earlier question"},
                    {"role": "assistant", "content": "Earlier answer"},
                    {"role": "user", "content": "Next question"},
                ],
            },
        )
        assert response.status_code == 200
        assert response.json()["text"] == "Actual answer"
        assert key not in response.text


@pytest.mark.parametrize("code", [302, 400, 401, 403, 429, 500])
def test_upstream_errors_and_redirects_never_echo_credentials(code: int) -> None:
    key = secrets.token_urlsafe(30)
    transport = httpx.MockTransport(lambda _: httpx.Response(code, text=key))
    with TestClient(create_web_app(transport=transport)) as client:
        connection = add(client, api_key=key).json()
        response = client.post(
            "/api/web/chat",
            headers=HEADERS,
            json={
                "connection_id": connection["id"],
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code in {429, 502}
        assert key not in response.text


def test_conversation_limit_and_missing_connections_do_not_call_provider() -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid input must not call a paid model")

    with TestClient(create_web_app(transport=httpx.MockTransport(forbidden))) as client:
        connection = add(client).json()
        for messages, status in [
            ([{"role": "assistant", "content": "Hi"}], 422),
            ([{"role": "user", "content": "a" * 24000}] * 3, 413),
        ]:
            response = client.post(
                "/api/web/chat",
                headers=HEADERS,
                json={
                    "connection_id": connection["id"],
                    "messages": messages,
                },
            )
            assert response.status_code == status
        assert (
            client.post(
                "/api/web/chat",
                headers=HEADERS,
                json={
                    "connection_id": "missing",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            ).status_code
            == 404
        )


def test_response_size_and_invalid_provider_json_are_bounded() -> None:
    responders: list[Callable[[httpx.Request], httpx.Response]] = [
        lambda _: httpx.Response(200, content=b"x" * 1_000_001),
        lambda _: httpx.Response(200, text="not JSON"),
        lambda _: httpx.Response(200, json={"choices": []}),
    ]
    for responder in responders:
        with TestClient(create_web_app(transport=httpx.MockTransport(responder))) as client:
            connection = add(client).json()
            response = client.post(
                "/api/web/chat",
                headers=HEADERS,
                json={
                    "connection_id": connection["id"],
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
            assert response.status_code == 502
