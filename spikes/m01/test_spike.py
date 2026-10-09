"""Offline tests for the M01 spike. Run: uv run pytest spikes/m01/test_spike.py"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import fakes  # noqa: E402
import run_spike as rs  # noqa: E402


def test_iter_sse_parses_events_comments_and_multiline() -> None:
    lines = [": keepalive", "event: a", "data: 1", "", "data: x", "data: y", "", "data: tail"]
    assert list(rs.iter_sse(lines)) == [("a", "1"), ("message", "x\ny"), ("message", "tail")]


def test_responses_stream_assembles_text_tools_and_timings() -> None:
    out = rs.StreamOutcome()
    raw = fakes.responses_text("Hello, world!") + fakes.responses_call_stream(
        "c1", "get_time", {"timezone": "UTC"}
    )
    for i, (name, data) in enumerate(rs.iter_sse(raw.decode().splitlines())):
        rs.feed_responses_event(out, name, data, float(i + 1))
    assert out.text == "Hello, world!"
    assert out.completed and out.reasoning_chars == 2
    assert out.tool_calls == [
        {"call_id": "c1", "name": "get_time", "arguments": '{"timezone": "UTC"}'}
    ]
    assert out.first_event_ms == 1.0
    assert out.first_token_ms is not None and out.first_token_ms <= out.first_content_ms  # type: ignore[operator]


def test_responses_failed_event_records_code() -> None:
    out = rs.StreamOutcome()
    data = json.dumps(
        {
            "type": "response.failed",
            "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}},
        }
    )
    rs.feed_responses_event(out, "response.failed", data, 5.0)
    assert out.failed_code == "subscription_sharing_usage_limit_exceeded" and not out.completed


def test_chat_stream_assembles_reasoning_and_fragmented_tool_call() -> None:
    out = rs.StreamOutcome()
    acc = rs.ChatAccumulator(out)
    raw = fakes.chat_call("t1", "add_todo", {"title": "x"})
    for name, data in rs.iter_sse(raw.decode().splitlines()):
        acc.feed(name, data, 3.0)
    assert out.completed and out.reasoning_chars > 0
    assert out.tool_calls == [{"call_id": "t1", "name": "add_todo", "arguments": '{"title": "x"}'}]
    assert out.assistant_message["reasoning_content"] == "need a tool"
    assert out.assistant_message["tool_calls"][0]["function"]["name"] == "add_todo"


def test_parse_error_extracts_code_and_redacts() -> None:
    body = json.dumps(
        {"error": {"code": "x", "type": "t", "message": "bad Bearer abc.def user a@b.io"}}
    )
    err = rs.parse_error(429, body)
    assert err["code"] == "x" and "abc.def" not in err["message"] and "a@b.io" not in err["message"]
    assert rs.parse_error(503, '{"detail": "down"}')["message"] == "down"


def test_sanitize_redacts_tokens_keys_and_emails() -> None:
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4eXoxMjM0NSJ9.c2lnbmF0dXJl"
    data = {
        "access_token": "whatever",
        "note": f"see {jwt} and me@x.org",
        "n": 3,
        "l": ["sk-abcdefghijklmnopqrstuv"],
    }
    clean = rs.sanitize(data)
    text = json.dumps(clean)
    assert "whatever" not in text and jwt not in text and "me@x.org" not in text
    assert "sk-abcdef" not in text and clean["n"] == 3
    with pytest.raises(RuntimeError):
        rs.assert_clean("leak: supersecretvalue", ["supersecretvalue"])


def test_rate_headers_filters_and_skips_cookies() -> None:
    headers = httpx.Headers(
        {"X-RateLimit-Remaining": "5", "Set-Cookie": "a=b", "Content-Type": "x"}
    )
    assert rs.rate_headers(headers) == {"x-ratelimit-remaining": "5"}


def test_authorize_url_matches_siwc_contract() -> None:
    url = rs.build_authorize_url("http://127.0.0.1:1455/auth/callback", "S", "N", "C", "urn:uuid:1")
    assert url.startswith("https://auth.openai.com/api/accounts/authorize?")
    assert (
        "scope=openid%20profile%20email%20offline_access%20resource.invoke%20chatgpt.tokens.use.direct"
        in url
    )
    assert "resource=https%3A%2F%2Fapi.openai.com%2Fv1" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback" in url
    assert "code_challenge_method=S256" in url and "client_id=dynamic_agent_client" in url


def test_pkce_challenge_is_s256_of_verifier() -> None:
    import base64
    import hashlib

    verifier, challenge = rs.pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode() and "=" not in challenge


def test_responses_body_omits_unsupported_fields() -> None:
    body = rs.responses_body("m", [{"role": "user", "content": "hi"}], True, encrypted=True)
    assert body["store"] is False and body["stream"] is True
    assert body["tools"][0]["type"] == "function" and body["include"] == [
        "reasoning.encrypted_content"
    ]
    for banned in ("temperature", "max_output_tokens", "previous_response_id", "metadata", "user"):
        assert banned not in body


def test_dry_run_end_to_end_writes_sanitized_results(tmp_path: Path) -> None:
    rc = rs.main(
        ["--provider", "all", "--dry-run", "--runs", "2", "--port", "0", "--out-dir", str(tmp_path)]
    )
    assert rc == 0
    files = sorted(tmp_path.glob("*.json"))
    assert {f.name.split("-")[0] for f in files} == {"gpt", "glm"}
    blob = "".join(f.read_text() for f in files)
    for secret in (
        fakes.FAKE_ACCESS,
        fakes.FAKE_ACCESS_2,
        fakes.FAKE_REFRESH_1,
        fakes.FAKE_REFRESH_2,
        "fake-deepinfra-key-for-dry-run",
        "a@b.example",
    ):
        assert secret not in blob
    gpt = json.loads(next(f for f in files if f.name.startswith("gpt")).read_text())
    oauth = gpt["steps"]["oauth"]
    assert (
        oauth["callback_state_ok"]
        and oauth["direct_scope_granted"]
        and oauth["refresh_token_rotated"]
    )
    assert oauth["id_token_nonce_ok"] and oauth["issued_client_id_returned"]
    model = gpt["steps"]["model:gpt-fake-1"]
    assert model["tool_calling"]["ok"] and model["tool_calling"]["tools_called"] == [
        "get_time",
        "add_todo",
    ]
    assert model["ttft"]["runs_ok"] == 2 and gpt["steps"]["models"]["hidden_count"] == 1
    assert gpt["steps"]["revoke"]["ok"] is True
    assert "x-ratelimit-remaining-requests" in gpt["rate_limit_headers"]
    assert "x-codex-primary-used-percent" in gpt["rate_limit_headers"]
    assert "set-cookie" not in gpt["rate_limit_headers"]
    glm = json.loads(next(f for f in files if f.name.startswith("glm")).read_text())
    assert glm["steps"]["tool_calling"]["ok"] and glm["steps"]["tool_calling"]["reasoning_seen"]
    assert glm["steps"]["catalog"]["pricing_usd_per_mtok"]["input_tokens"] == 0.15


def test_oauth_state_mismatch_is_rejected() -> None:
    world = fakes.FakeWorld()

    def evil_browser(url: str) -> None:
        redirect = rs.parse_qs(rs.urlsplit(url).query)["redirect_uri"][0]
        httpx.get(f"{redirect}?code=c&state=WRONG&client_id=oaiapp_x")

    with httpx.Client(transport=httpx.MockTransport(world.handler)) as client:
        with pytest.raises(RuntimeError, match="authorization failed"):
            rs.sign_in(client, evil_browser, 5.0, "urn:uuid:1", 0, {})


def test_sign_in_does_not_hang_when_browser_leaves_an_idle_connection() -> None:
    """Regression: browsers open extra idle (preconnect) sockets to the loopback listener.

    A single-threaded server blocks reading such a socket, so shutdown() deadlocked after the
    callback had already been received.
    """
    import socket
    import threading
    import time

    world = fakes.FakeWorld()
    idle: list[socket.socket] = []

    def browser(url: str) -> None:
        world.open_browser(url)  # real callback request, answered
        port = int(
            rs.parse_qs(rs.urlsplit(url).query)["redirect_uri"][0].split(":")[2].split("/")[0]
        )
        sock = socket.create_connection(("127.0.0.1", port))  # speculative connection, no bytes
        idle.append(sock)
        time.sleep(0.4)  # let the server pick the idle socket up

    result: dict[str, object] = {}

    def run() -> None:
        with httpx.Client(transport=httpx.MockTransport(world.handler)) as client:
            result["session"] = rs.sign_in(client, browser, 5.0, "urn:uuid:1", 0, {})

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(8.0)
    for sock in idle:
        sock.close()
    assert not worker.is_alive(), "sign_in hung after the callback (server shutdown deadlock)"
    assert "session" in result
