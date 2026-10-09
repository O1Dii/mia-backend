"""Fake OpenAI/SIWC/DeepInfra world for `--dry-run` and the offline tests. No real secrets."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

FAKE_ACCESS = "fake-access-token-AAAAAAAAAAAAAAAAAAAA"
FAKE_REFRESH_1 = "fake-refresh-token-1-BBBBBBBBBBBBBBBB"
FAKE_REFRESH_2 = "fake-refresh-token-2-CCCCCCCCCCCCCCCC"
FAKE_ACCESS_2 = "fake-access-token-2-DDDDDDDDDDDDDDDDDD"
FAKE_CLIENT_ID = "oaiapp_fake123"
FULL_SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


def sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def b64(obj: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def responses_text(text: str) -> bytes:
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"type": "response.created", "response": {}}),
        (
            "response.reasoning_summary_text.delta",
            {"type": "response.reasoning_summary_text.delta", "delta": "hm"},
        ),
    ]
    for chunk in (text[: len(text) // 2], text[len(text) // 2 :]):
        events.append(
            ("response.output_text.delta", {"type": "response.output_text.delta", "delta": chunk})
        )
    events.append(
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 12, "output_tokens": 7}},
            },
        )
    )
    return sse(events)


def responses_call_stream(call_id: str, name: str, arguments: dict[str, Any]) -> bytes:
    args = json.dumps(arguments)
    item = {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": args,
    }
    return sse(
        [
            ("response.output_item.added", {"type": "response.output_item.added", "item": item}),
            (
                "response.function_call_arguments.delta",
                {"type": "response.function_call_arguments.delta", "delta": args[:5]},
            ),
            ("response.output_item.done", {"type": "response.output_item.done", "item": item}),
            ("response.completed", {"type": "response.completed", "response": {"usage": {}}}),
        ]
    )


def chat_chunks(chunks: list[dict[str, Any]]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def chat_text(text: str) -> bytes:
    return chat_chunks(
        [
            {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {"choices": [{"delta": {"content": text[:5]}}]},
            {"choices": [{"delta": {"content": text[5:]}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 9}},
        ]
    )


def chat_call(call_id: str, name: str, arguments: dict[str, Any]) -> bytes:
    args = json.dumps(arguments)
    return chat_chunks(
        [
            {"choices": [{"delta": {"reasoning_content": "need a tool"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "function": {"name": name, "arguments": args[:6]},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": args[6:]}}]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
    )


class FakeWorld:
    """Routes httpx requests to canned responses; drives the real loopback callback server."""

    def __init__(self) -> None:
        self.nonce = ""
        self.refreshed = False
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []
        self.revoked = False

    def open_browser(self, url: str) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["client_id"] == "dynamic_agent_client"
        assert query["code_challenge_method"] == "S256"
        assert query["redirect_uri"].startswith("http://127.0.0.1:")
        self.nonce = query["nonce"]
        callback = (
            f"{query['redirect_uri']}?code=fake-code&state={query['state']}"
            f"&client_id={FAKE_CLIENT_ID}"
        )
        httpx.get(callback)  # real loopback request to the real listener

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append((request.method, f"{url.host}{url.path}"))
        path = url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.openai.com",
                    "authorization_endpoint": "https://auth.openai.com/api/accounts/authorize",
                    "token_endpoint": "https://auth.openai.com/api/accounts/oauth/token",
                    "revocation_endpoint": "https://auth.openai.com/api/accounts/oauth/revoke",
                    "scopes_supported": ["openid", "offline_access"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path.endswith("/oauth/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form["grant_type"] == "authorization_code":
                assert form["code_verifier"] and form["resource"] == "https://api.openai.com/v1"
                claims = {
                    "iss": "https://auth.openai.com",
                    "aud": FAKE_CLIENT_ID,
                    "nonce": self.nonce,
                    "email": "a@b.example",
                }
                id_token = f"h.{b64(claims)}.s"
                return httpx.Response(
                    200,
                    json={
                        "access_token": FAKE_ACCESS,
                        "refresh_token": FAKE_REFRESH_1,
                        "id_token": id_token,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": FULL_SCOPE,
                        "earliest_refresh_at": 1,
                    },
                )
            assert form["refresh_token"] == FAKE_REFRESH_1
            self.refreshed = True
            return httpx.Response(
                200,
                json={
                    "access_token": FAKE_ACCESS_2,
                    "refresh_token": FAKE_REFRESH_2,
                    "expires_in": 3600,
                    "scope": "openid offline_access resource.invoke chatgpt.tokens.use.direct",
                },
            )
        if path.endswith("/oauth/revoke"):
            self.revoked = True
            return httpx.Response(200)
        if url.host == "api.openai.com" and path.endswith("/models"):
            assert request.headers["authorization"] == f"Bearer {FAKE_ACCESS_2}"
            return httpx.Response(
                200,
                headers={"x-ratelimit-remaining-requests": "99", "set-cookie": "s=1"},
                json={
                    "models": [
                        {"slug": "gpt-fake-1", "visibility": "list"},
                        {"slug": "gpt-hidden", "visibility": "hide"},
                    ]
                },
            )
        if url.host == "api.openai.com" and path.endswith("/responses"):
            return self._responses(request)
        if url.host == "api.deepinfra.com" and path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "zai-org/GLM-5.3-Flash",
                            "metadata": {
                                "context_length": 1048576,
                                "pricing": {"input_tokens": 0.15, "output_tokens": 0.5},
                                "tags": ["reasoning"],
                            },
                        }
                    ]
                },
            )
        if url.host == "api.deepinfra.com" and path.endswith("/chat/completions"):
            return self._chat(request)
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def _responses(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        assert body["store"] is False and body["stream"] is True
        for banned in ("temperature", "max_output_tokens", "previous_response_id"):
            assert banned not in body
        headers = {"x-codex-primary-used-percent": "3"}
        outputs = [i for i in body["input"] if i.get("type") == "function_call_output"]
        if "tools" not in body:
            return httpx.Response(200, headers=headers, content=responses_text("Hello, world!"))
        if len(outputs) == 0:
            return httpx.Response(
                200,
                headers=headers,
                content=responses_call_stream("c1", "get_time", {"timezone": "UTC"}),
            )
        if len(outputs) == 1:
            return httpx.Response(
                200,
                headers=headers,
                content=responses_call_stream("c2", "add_todo", {"title": "Call Anna"}),
            )
        return httpx.Response(200, headers=headers, content=responses_text("Done, todo added."))

    def _chat(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        assert body["stream"] is True
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        headers = {"x-ratelimit-limit-requests": "200"}
        if "tools" not in body:
            return httpx.Response(200, headers=headers, content=chat_text("Hello, world!"))
        if not tool_msgs:
            return httpx.Response(
                200, headers=headers, content=chat_call("t1", "get_time", {"timezone": "UTC"})
            )
        if len(tool_msgs) == 1:
            # reasoning_content from the first assistant turn must be replayed
            assistant = next(m for m in body["messages"] if m["role"] == "assistant")
            assert assistant.get("reasoning_content")
            return httpx.Response(
                200, headers=headers, content=chat_call("t2", "add_todo", {"title": "Call Anna"})
            )
        return httpx.Response(200, headers=headers, content=chat_text("Done, todo added."))
