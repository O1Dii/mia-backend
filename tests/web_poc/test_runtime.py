"""Actual API -> selected adapter -> action runtime -> shared view state contract tests."""

import io
import json
import secrets
import socket
import time
import zipfile
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from app.web_poc.internet import Internet, InternetError, fetch_public, parse_feed, public_addresses
from app.web_poc.main import create_web_app
from app.web_poc.providers import PROVIDERS
from app.web_poc.runtime import AppManifest, PluginRuntime, Workspace, community_tool_name
from fastapi.testclient import TestClient

HEADERS = {"X-Mia-Web": "1", "Origin": "http://127.0.0.1:5173"}


def post(client: TestClient, path: str, body: dict[str, Any]) -> httpx.Response:
    return cast(httpx.Response, client.post("/api/web" + path, json=body, headers=HEADERS))


def connect(client: TestClient, provider: str = "deepinfra", **extra: Any) -> str:
    response = post(
        client,
        "/connections",
        {
            "provider": provider,
            "label": "Selected GLM",
            "model": "zai-org/GLM-5.3-Flash",
            "api_key": secrets.token_urlsafe(24),
            **extra,
        },
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def native_reply(provider: str, *, final: bool = False, arguments: Any = None) -> dict[str, Any]:
    args = arguments if arguments is not None else {"seconds": 300, "label": "Чай"}
    if provider == "openai":
        return {
            "output": (
                [{"type": "message", "content": [{"type": "output_text", "text": "Готово"}]}]
                if final
                else [
                    {"type": "reasoning", "id": "r1", "encrypted_content": "opaque", "summary": []},
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "mia_timer__start",
                        "arguments": json.dumps(args),
                    },
                ]
            )
        }
    if provider == "anthropic":
        return {
            "content": (
                [{"type": "text", "text": "Готово"}]
                if final
                else [
                    {"type": "tool_use", "id": "call-1", "name": "mia_timer__start", "input": args}
                ]
            )
        }
    if provider == "gemini":
        return {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": (
                            [{"text": "Готово"}]
                            if final
                            else [
                                {
                                    "thoughtSignature": "opaque-signature",
                                    "functionCall": {"name": "mia_timer__start", "args": args},
                                }
                            ]
                        ),
                    }
                }
            ]
        }
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "Готово" if final else "",
        "reasoning_content": "opaque-reasoning",
    }
    if not final:
        message["tool_calls"] = [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "mia_timer__start",
                    "arguments": args if provider == "ollama" else json.dumps(args),
                },
            }
        ]
    return {"message": message} if provider == "ollama" else {"choices": [{"message": message}]}


@pytest.mark.parametrize("provider", list(PROVIDERS))
def test_selected_model_executes_timer_and_preserves_native_continuation(provider: str) -> None:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert "mia_timer__start" in request.content.decode()
            assert "mia_notes__add" not in request.content.decode()
        else:
            raw = request.content.decode()
            assert "deadline" in raw
            if provider == "gemini":
                assert "opaque-signature" in raw
            if provider == "openai":
                assert "opaque" in raw
            if provider in {"deepinfra", "local", "groq", "mistral", "ollama"}:
                assert "opaque-reasoning" in raw
        return httpx.Response(200, json=native_reply(provider, final=len(requests) > 1))

    with TestClient(create_web_app(transport=httpx.MockTransport(respond))) as client:
        connection = connect(client, provider)
        post(client, "/plugins/install", {"plugin_id": "mia.timer"})
        command: dict[str, Any] = {
            "connection_id": connection,
            "request_id": "timer-turn-1",
            "messages": [{"role": "user", "content": "Поставь таймер на 5 минут"}],
        }
        result = post(client, "/chat", command)
        assert result.status_code == 200, result.text
        assert result.json()["actions"][0]["ok"]
        state = client.get("/api/web/workspace").json()
        assert 295000 < state["states"]["mia.timer"]["deadline"] - time.time() * 1000 <= 300000
        assert state["layout"][0]["plugin_id"] == "mia.timer"
        assert post(client, "/chat", command).json() == result.json()
        assert len(requests) == 2  # Retransmission cannot restart the countdown.
        command["messages"][0]["content"] = "Другой запрос"
        assert post(client, "/chat", command).status_code == 409


def test_invalid_action_and_uninstalled_tools_cannot_change_state() -> None:
    with TestClient(create_web_app()) as client:
        command: dict[str, Any] = {
            "name": "mia_timer__start",
            "arguments": {"seconds": 300, "label": "Test"},
            "request_id": "stable-id",
        }
        assert not post(client, "/actions", command).json()["ok"]
        post(client, "/plugins/install", {"plugin_id": "mia.timer"})
        for seconds in [0, -1, 10801, "300", True]:
            assert not post(
                client,
                "/actions",
                {
                    **command,
                    "request_id": secrets.token_hex(8),
                    "arguments": {"seconds": seconds, "label": "Test"},
                },
            ).json()["ok"]
        first = post(client, "/actions", command).json()
        assert first["ok"]
        assert post(client, "/actions", command).json()["data"] == first["data"]
        client.delete("/api/web/plugins/mia.timer", headers=HEADERS)
        assert not post(client, "/actions", command).json()["ok"]


def test_json_only_model_uses_validated_protocol_and_real_state() -> None:
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        assert "tools" not in json.loads(request.content)
        text = json.dumps(
            {"action": "mia_timer__start", "arguments": {"seconds": 300, "label": "JSON"}}
            if count == 1
            else {"answer": "Таймер запущен"}
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    with TestClient(create_web_app(transport=httpx.MockTransport(respond))) as client:
        connect(client, "local", api_key="", tool_mode="json")
        post(client, "/plugins/install", {"plugin_id": "mia.timer"})
        result = post(client, "/chat", {"messages": [{"role": "user", "content": "5 минут"}]})
        assert result.json()["actions"][0]["ok"]
        assert client.get("/api/web/workspace").json()["states"]["mia.timer"]["label"] == "JSON"
        assert count == 2


def pair(client: TestClient, plugin_id: str) -> tuple[str, str]:
    start = client.post("/api/sdk/pair", json={"plugin_id": plugin_id}).json()
    assert client.post("/api/sdk/pair/poll", json={"device_code": start["device_code"]}).json() == {
        "status": "pending"
    }
    approved = post(client, "/apps/approve", {"user_code": start["user_code"]})
    assert approved.status_code == 200
    token = client.post("/api/sdk/pair/poll", json={"device_code": start["device_code"]}).json()[
        "access_token"
    ]
    assert (
        client.post("/api/sdk/pair/poll", json={"device_code": start["device_code"]}).status_code
        == 410
    )
    return token, approved.json()["id"]


def test_external_app_shared_state_selected_model_scope_revocation_and_isolation() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "current-model"
        assert "mia_timer__start" in request.content.decode()
        assert "mia_notes__add" not in request.content.decode()
        final = any(m["role"] == "tool" for m in payload["messages"])
        return httpx.Response(200, json=native_reply("deepinfra", final=final))

    app = create_web_app(transport=httpx.MockTransport(respond))
    with TestClient(app) as owner, TestClient(app) as stranger:
        connect(owner)
        current = connect(owner, model="current-model")
        post(owner, "/connections/select", {"connection_id": current})
        for p in ["mia.timer", "mia.notes"]:
            post(owner, "/plugins/install", {"plugin_id": p})
        token, grant_id = pair(owner, "mia.timer")
        bearer = {"Authorization": f"Bearer {token}"}
        assert stranger.get("/api/sdk/state").status_code == 401
        result = stranger.post(
            "/api/sdk/generate",
            headers=bearer,
            json={
                "connection_id": "ignored-foreign-id",
                "messages": [{"role": "user", "content": "Таймер 5 минут"}],
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["actions"][0]["ok"]
        sdk = stranger.get("/api/sdk/state", headers=bearer).json()
        web = owner.get("/api/web/workspace").json()
        assert sdk["state"] == web["states"]["mia.timer"]
        assert sdk["subject_id"] == web["subject_id"]
        assert stranger.get("/api/web/workspace").json()["installed"] == []
        assert not stranger.post(
            "/api/sdk/actions",
            headers=bearer,
            json={"name": "mia_notes__add", "arguments": {"text": "unauthorized"}},
        ).json()["ok"]
        assert stranger.delete("/api/web/apps/" + grant_id, headers=HEADERS).status_code == 404
        assert owner.delete("/api/web/apps/" + grant_id, headers=HEADERS).status_code == 204
        assert stranger.get("/api/sdk/state", headers=bearer).status_code == 401
        token, _ = pair(owner, "mia.timer")
        owner.delete("/api/web/plugins/mia.timer", headers=HEADERS)
        post(owner, "/plugins/install", {"plugin_id": "mia.timer"})
        assert (
            stranger.get("/api/sdk/state", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )


class News(Internet):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.empty = False

    async def search(self, query: str, *, news: bool = False) -> list[dict[str, Any]]:
        assert news
        self.calls.append(query)
        return (
            []
            if self.empty
            else [
                {
                    "title": "Новая модель",
                    "url": "https://example.org/news",
                    "source": "Publisher",
                    "published_at": format_datetime(datetime.now(UTC)),
                    "snippet": "Опубликован релиз.",
                }
            ]
        )


def test_digest_collects_news_and_summarizes_with_selected_model() -> None:
    internet = News()

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "selected-news-model"
        assert "https://example.org/news" in payload["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "Вышел релиз [1]."}}]})

    with TestClient(
        create_web_app(transport=httpx.MockTransport(respond), internet=internet)
    ) as client:
        connect(client, model="selected-news-model")
        installed = post(client, "/plugins/install", {"plugin_id": "mia.digest"}).json()
        assert set(installed["installed"]) == {"mia.digest", "mia.web"}
        assert client.delete("/api/web/plugins/mia.web", headers=HEADERS).status_code == 409
        result = post(
            client, "/actions", {"name": "mia_digest__refresh", "arguments": {"topics": "ИИ"}}
        ).json()
        assert result["ok"] and result["data"]["summary"] == "Вышел релиз [1]."
        assert internet.calls == ["ИИ"]
        assert (
            post(client, "/digest/settings", {"topics": "ИИ", "interval_minutes": 30}).status_code
            == 200
        )
        internet.empty = True
        result = post(
            client, "/actions", {"name": "mia_digest__refresh", "arguments": {"topics": "ИИ"}}
        ).json()
        assert not result["ok"]
        assert result["workspace"]["states"]["mia.digest"]["summary"] == "Вышел релиз [1]."


def test_public_web_blocks_private_addresses_and_unsafe_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for host in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "224.0.0.1"]:
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda *args, host=host, **kwargs: [(0, 0, 0, "", (host, 443))]
        )
        with pytest.raises(InternetError):
            public_addresses("example.org")
    for url in ["http://example.org", "https://user:pass@example.org", "https://example.org:8001"]:
        with pytest.raises(InternetError):
            fetch_public(url)


def test_news_freshness_html_stripping_and_xml_entities() -> None:
    def item(title: str, when: datetime) -> str:
        return f"<item><title>{title}</title><link>https://example.org</link><pubDate>{format_datetime(when)}</pubDate><description>&lt;script&gt;bad&lt;/script&gt;&lt;p&gt;news&lt;/p&gt;</description></item>"

    body = (
        "<rss><channel>"
        + item("new", datetime.now(UTC))
        + item("old", datetime.now(UTC) - timedelta(days=4))
        + "</channel></rss>"
    ).encode()
    items = parse_feed(body, news=True)
    assert len(items) == 1 and items[0]["snippet"] == "news"
    with pytest.raises(InternetError):
        parse_feed(b"<!DOCTYPE rss><rss/>", news=True)


async def test_cached_action_cannot_bypass_plugin_scope() -> None:
    workspace = Workspace(expires=time.monotonic() + 60)
    workspace.install("mia.notes")
    workspace.install("mia.timer")
    runtime = PluginRuntime()
    command = ("mia_notes__add", {"text": "private"}, "same-call")
    assert (await runtime.execute(workspace, *command))["ok"]
    assert not (await runtime.execute(workspace, *command, only_plugin="mia.timer"))["ok"]


def test_author_manifest_actions_and_distribution_bundle() -> None:
    manifest = json.loads(Path("contracts/plugins/v2/examples/focus-board.json").read_text())
    with TestClient(create_web_app()) as client:
        assert (
            post(
                client, "/plugins/install", {"plugin_id": manifest["id"], "manifest": manifest}
            ).status_code
            == 200
        )
        name = community_tool_name(manifest["id"], "set_focus")
        result = post(
            client, "/actions", {"name": name, "arguments": {"task": "Сделать web"}}
        ).json()
        assert result["ok"] and result["data"]["task"] == "Сделать web"
        invalid = post(
            client, "/actions", {"name": name, "arguments": {"task": "x", "system": "bad"}}
        ).json()
        assert not invalid["ok"]
        with zipfile.ZipFile(
            io.BytesIO(client.get("/api/web/downloads/timer.zip").content)
        ) as archive:
            assert set(archive.namelist()) == {"README.md", "mia-sdk.mjs", "timer.html"}
        assert client.get("/api/web/sdk/timer.html").status_code == 200
        manifest["permissions"] = ["state.read"]
        with pytest.raises(ValueError):
            AppManifest.model_validate(manifest)


def test_sdk_cors_and_cookie_api_boundary() -> None:
    with TestClient(create_web_app()) as client:
        preflight = client.options(
            "/api/sdk/state",
            headers={
                "Origin": "http://127.0.0.1:8088",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert preflight.status_code == 200
        assert "access-control-allow-credentials" not in preflight.headers
        assert client.post("/api/web/actions", json={}).status_code == 403


def test_partial_provider_failure_preserves_action_and_prevents_blind_replay() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(200, json=native_reply("deepinfra"))
            if calls == 1
            else httpx.Response(503, json={"error": "temporary"})
        )

    with TestClient(create_web_app(transport=httpx.MockTransport(respond))) as client:
        connect(client)
        post(client, "/plugins/install", {"plugin_id": "mia.timer"})
        command = {
            "request_id": "partial-turn",
            "messages": [{"role": "user", "content": "5 минут"}],
        }
        first = post(client, "/chat", command).json()
        assert first["actions"][0]["ok"] and first["warning"]
        assert post(client, "/chat", command).json() == first
        assert calls == 2


def test_duplicate_native_call_id_does_not_repeat_timer_action() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=native_reply("deepinfra", final=calls == 3))

    with TestClient(create_web_app(transport=httpx.MockTransport(respond))) as client:
        connect(client)
        post(client, "/plugins/install", {"plugin_id": "mia.timer"})
        result = post(client, "/chat", {"messages": [{"role": "user", "content": "Таймер"}]}).json()
        assert len(result["actions"]) == 1
        assert len(client.get("/api/web/workspace").json()["audit"]) == 1
        assert calls == 3


def test_invalid_json_model_never_mutates_state() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "Таймер запущен!"}}]})

    with TestClient(create_web_app(transport=httpx.MockTransport(respond))) as client:
        connect(client, tool_mode="json")
        post(client, "/plugins/install", {"plugin_id": "mia.timer"})
        result = post(
            client, "/chat", {"messages": [{"role": "user", "content": "5 минут"}]}
        ).json()
        assert result["actions"] == [] and result["warning"]
        assert client.get("/api/web/workspace").json()["states"]["mia.timer"] == {}


def test_manifest_upgrade_revokes_existing_external_clients() -> None:
    manifest = json.loads(Path("contracts/plugins/v2/examples/focus-board.json").read_text())
    with TestClient(create_web_app()) as client:
        post(client, "/plugins/install", {"plugin_id": manifest["id"], "manifest": manifest})
        token, _ = pair(client, manifest["id"])
        manifest["version"] = "1.1.0"
        post(client, "/plugins/install", {"plugin_id": manifest["id"], "manifest": manifest})
        assert (
            client.get("/api/sdk/state", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )
