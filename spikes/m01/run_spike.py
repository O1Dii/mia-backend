#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""M01 spike: main model choice (GPT on a ChatGPT plan via SIWC vs DeepInfra GLM-5.3-Flash).

Run in your own terminal so no secret ever passes through a chat:

    uv run --script spikes/m01/run_spike.py --provider gpt-plan|glm|all

Tokens and keys live in memory only. Results are sanitized before writing to
``spikes/m01/results/<provider>-<timestamp>.json``. ``--dry-run`` uses fakes (no network, no
browser, no credentials) and is what the offline tests exercise.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import statistics
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

ISSUER = "https://auth.openai.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
REVOKE_URL = f"{ISSUER}/api/accounts/oauth/revoke"
OPENAI_API = "https://api.openai.com/v1"
DEEPINFRA_API = "https://api.deepinfra.com/v1/openai"
GLM_MODEL = "zai-org/GLM-5.3-Flash"
DYNAMIC_CLIENT = "dynamic_agent_client"
AGENT_NAME = "MIA-spike-M01"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
DEFAULT_PORT = 1455

INSTRUCTIONS = "You are a concise assistant. Use the provided tools when the user asks."
TTFT_PROMPT = "Say exactly: Hello, world!"
TOOL_PROMPT = (
    "First call get_time for timezone UTC. Then call add_todo with the title "
    "'Call Anna' and the due time one hour after the time you got. "
    "Finally confirm in one short sentence."
)
MAX_TURNS = 5
STREAM_DEADLINE_S = 120.0  # hard cap per streamed request, on top of the 60s read timeout

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_time",
        "description": "Return the current time in an IANA timezone.",
        "parameters": {
            "type": "object",
            "properties": {"timezone": {"type": "string"}},
            "required": ["timezone"],
        },
    },
    {
        "name": "add_todo",
        "description": "Add a todo item.",
        "parameters": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "due": {"type": "string"}},
            "required": ["title"],
        },
    },
]

# ---------------------------------------------------------------- sanitizing

RATE_HEADER_RE = re.compile(
    r"(rate|limit|remaining|reset|retry|usage|quota|codex|used|percent|window|plan|credit)", re.I
)
SECRET_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),  # JWT
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bBearer\s+\S+", re.I),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # e-mail
    re.compile(r"\b[A-Za-z0-9_-]{40,}\b"),  # long opaque blobs
]
SECRET_KEY_RE = re.compile(r"(token|secret|password|authorization|cookie|api_?key)", re.I)


def say(message: str) -> None:
    """One-line progress message. Callers must never pass secrets."""
    print(f"[m01] {message}", flush=True)


def redact_text(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def sanitize(value: Any) -> Any:
    """Recursively redact secret-looking strings and secret-named keys."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(k): "[redacted]"
            if SECRET_KEY_RE.search(str(k)) and isinstance(v, str)
            else sanitize(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize(v) for v in value]
    return value


def assert_clean(text: str, live_secrets: Iterable[str]) -> None:
    """Fail loudly if any known live secret appears in text about to be written."""
    for secret in live_secrets:
        if secret and len(secret) >= 8 and secret in text:
            raise RuntimeError("sanitization failure: a live secret reached the output")


def rate_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        k.lower(): redact_text(v)[:200]
        for k, v in headers.items()
        if RATE_HEADER_RE.search(k) and "cookie" not in k.lower()
    }


# ----------------------------------------------------------------------- SSE


def iter_sse(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """Minimal SSE parser over already split lines. Yields (event, data)."""
    event = "message"
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:][1:] if line[5:6] == " " else line[5:])
    if data:
        yield event, "\n".join(data)


@dataclass
class StreamOutcome:
    """What one streamed request produced. Timings are ms since the request was sent."""

    text: str = ""
    reasoning_chars: int = 0
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    output_items: list[dict[str, Any]] = field(default_factory=list)
    assistant_message: dict[str, Any] = field(default_factory=dict)
    completed: bool = False
    failed_code: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    event_types: list[str] = field(default_factory=list)
    first_event_ms: float | None = None
    first_reasoning_ms: float | None = None
    first_content_ms: float | None = None
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    http_error: dict[str, Any] | None = None

    @property
    def first_token_ms(self) -> float | None:
        marks = [m for m in (self.first_reasoning_ms, self.first_content_ms) if m is not None]
        return min(marks) if marks else None

    def mark_event(self, name: str, now_ms: float) -> None:
        if self.first_event_ms is None:
            self.first_event_ms = now_ms
        if name not in self.event_types:
            self.event_types.append(name)

    def mark_content(self, now_ms: float) -> None:
        if self.first_content_ms is None:
            self.first_content_ms = now_ms

    def mark_reasoning(self, now_ms: float, chars: int) -> None:
        self.reasoning_chars += chars
        if self.first_reasoning_ms is None:
            self.first_reasoning_ms = now_ms


def parse_error(status: int, body: str) -> dict[str, Any]:
    """Reduce an upstream error body to status + code/type + a short sanitized message."""
    code = etype = message = None
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code, etype, message = err.get("code"), err.get("type"), err.get("message")
        elif isinstance(err, str):
            message = err
        elif "detail" in payload:
            message = str(payload["detail"])
    return {
        "status": status,
        "code": str(code) if code else None,
        "type": str(etype) if etype else None,
        "message": redact_text(str(message))[:200] if message else None,
    }


def feed_responses_event(out: StreamOutcome, name: str, raw: str, now_ms: float) -> None:
    """Accumulate one Responses API SSE event."""
    try:
        data = json.loads(raw)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    etype = str(data.get("type") or name)
    out.mark_event(etype, now_ms)
    if etype == "response.output_text.delta":
        out.text += str(data.get("delta", ""))
        out.mark_content(now_ms)
    elif "reasoning" in etype and etype.endswith(".delta"):
        out.mark_reasoning(now_ms, len(str(data.get("delta", ""))))
    elif etype == "response.function_call_arguments.delta":
        out.mark_content(now_ms)
    elif etype == "response.output_item.done":
        item = data.get("item")
        if isinstance(item, dict):
            out.output_items.append(item)
            if item.get("type") == "function_call":
                out.tool_calls.append(
                    {
                        "call_id": str(item.get("call_id", "")),
                        "name": str(item.get("name", "")),
                        "arguments": str(item.get("arguments", "")),
                    }
                )
    elif etype == "response.completed":
        out.completed = True
        response = data.get("response") or {}
        out.usage = response.get("usage") or {}
    elif etype in ("response.failed", "error"):
        err = (data.get("response") or {}).get("error") or data.get("error") or data
        out.failed_code = str(err.get("code") or err.get("type") or "unknown_error")


class ChatAccumulator:
    """Assemble an OpenAI-compatible chat.completions stream (DeepInfra GLM)."""

    def __init__(self, out: StreamOutcome) -> None:
        self.out = out
        self.calls: dict[int, dict[str, str]] = {}
        self.reasoning = ""

    def feed(self, _name: str, raw: str, now_ms: float) -> None:
        out = self.out
        if raw.strip() == "[DONE]":
            out.mark_event("[DONE]", now_ms)
            self.finish()
            return
        try:
            chunk = json.loads(raw)
        except ValueError:
            return
        out.mark_event("chunk", now_ms)
        if isinstance(chunk.get("usage"), dict):
            out.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                self.reasoning += reasoning
                out.mark_reasoning(now_ms, len(reasoning))
            if delta.get("content"):
                out.text += delta["content"]
                out.mark_content(now_ms)
            for call in delta.get("tool_calls") or []:
                slot = self.calls.setdefault(
                    int(call.get("index", 0)), {"call_id": "", "name": "", "arguments": ""}
                )
                fn = call.get("function") or {}
                slot["call_id"] = call.get("id") or slot["call_id"]
                slot["name"] = fn.get("name") or slot["name"]
                slot["arguments"] += fn.get("arguments") or ""
                out.mark_content(now_ms)

    def finish(self) -> None:
        out = self.out
        out.completed = True
        out.tool_calls = [self.calls[i] for i in sorted(self.calls)]
        message: dict[str, Any] = {"role": "assistant", "content": out.text or None}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if out.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c["call_id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for c in out.tool_calls
            ]
        out.assistant_message = message


def stream_request(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    feed: Callable[[str, str, float], None],
    out: StreamOutcome,
) -> StreamOutcome:
    start = time.perf_counter()
    with client.stream("POST", url, headers=headers, json=body) as response:
        out.status = response.status_code
        out.headers = rate_headers(response.headers)
        if response.status_code >= 400:
            response.read()
            out.http_error = parse_error(response.status_code, response.text)
            return out
        for name, data in iter_sse(response.iter_lines()):
            feed(name, data, (time.perf_counter() - start) * 1000)
            if time.perf_counter() - start > STREAM_DEADLINE_S:
                out.http_error = {"status": 0, "code": "stream_deadline_exceeded", "type": None}
                break
    return out


# --------------------------------------------------------------------- tools


def execute_tool(name: str, arguments: str) -> tuple[str, bool]:
    """Fake tool backend. Returns (json output, arguments_were_valid)."""
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return json.dumps({"error": "invalid json arguments"}), False
    if name == "get_time":
        valid = isinstance(args.get("timezone"), str)
        return json.dumps({"timezone": args.get("timezone"), "now": "2026-01-01T12:00:00Z"}), valid
    if name == "add_todo":
        valid = isinstance(args.get("title"), str)
        return json.dumps({"id": 1, "status": "added", "title": args.get("title")}), valid
    return json.dumps({"error": f"unknown tool {name}"}), False


@dataclass
class ToolScenario:
    ok: bool = False
    tools_called: list[str] = field(default_factory=list)
    args_valid: bool = True
    turns: list[dict[str, Any]] = field(default_factory=list)
    final_text_nonempty: bool = False
    reasoning_seen: bool = False
    error: dict[str, Any] | None = None
    continuation_mode: str | None = None

    def record_turn(self, out: StreamOutcome) -> None:
        self.turns.append(
            {
                "tool_calls": [c["name"] for c in out.tool_calls],
                "first_event_ms": _r(out.first_event_ms),
                "first_token_ms": _r(out.first_token_ms),
                "completed": out.completed,
                "reasoning_chars": out.reasoning_chars,
                "usage": out.usage,
            }
        )
        self.reasoning_seen = self.reasoning_seen or out.reasoning_chars > 0

    def finalize(self) -> dict[str, Any]:
        names = set(self.tools_called)
        self.ok = (
            self.error is None
            and {"get_time", "add_todo"} <= names
            and self.args_valid
            and self.final_text_nonempty
        )
        return {
            "ok": self.ok,
            "tools_called": self.tools_called,
            "arguments_valid_json": self.args_valid,
            "final_text_nonempty": self.final_text_nonempty,
            "reasoning_seen": self.reasoning_seen,
            "continuation_mode": self.continuation_mode,
            "turns": self.turns,
            "error": self.error,
        }


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _headers_merge(into: dict[str, str], new: dict[str, str]) -> None:
    into.update(new)


# ----------------------------------------------------------- Responses (GPT)


def responses_body(
    model: str, items: list[dict[str, Any]], tools: bool, encrypted: bool = False
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": items,
        "store": False,
        "stream": True,
    }
    if tools:
        body["tools"] = [{"type": "function", **spec} for spec in TOOL_SPECS]
        body["tool_choice"] = "auto"
    if encrypted:
        body["include"] = ["reasoning.encrypted_content"]
    return body


def responses_call(
    client: httpx.Client, token: str, body: dict[str, Any], rl: dict[str, str]
) -> StreamOutcome:
    out = StreamOutcome()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    stream_request(
        client,
        f"{OPENAI_API}/responses",
        headers,
        body,
        lambda n, d, t: feed_responses_event(out, n, d, t),
        out,
    )
    _headers_merge(rl, out.headers)
    return out


def responses_tool_loop(
    client: httpx.Client, token: str, model: str, mode: str, rl: dict[str, str]
) -> ToolScenario:
    """mode 'plain' drops reasoning items; 'encrypted' replays them via encrypted_content."""
    scenario = ToolScenario(continuation_mode=mode)
    items: list[dict[str, Any]] = [{"role": "user", "content": TOOL_PROMPT}]
    for _ in range(MAX_TURNS):
        out = responses_call(
            client, token, responses_body(model, items, True, mode == "encrypted"), rl
        )
        scenario.record_turn(out)
        say(f"tool turn {len(scenario.turns)}: {[c['name'] for c in out.tool_calls] or 'text'}")
        if out.http_error or out.failed_code or not out.completed:
            scenario.error = out.http_error or {"status": out.status, "code": out.failed_code}
            return scenario
        if not out.tool_calls:
            scenario.final_text_nonempty = bool(out.text.strip())
            return scenario
        for item in out.output_items:
            if item.get("type") == "reasoning" and mode == "encrypted":
                items.append(item)
            elif item.get("type") == "function_call":
                items.append({k: item[k] for k in ("type", "call_id", "name", "arguments")})
        for call in out.tool_calls:
            result, valid = execute_tool(call["name"], call["arguments"])
            scenario.tools_called.append(call["name"])
            scenario.args_valid = scenario.args_valid and valid
            items.append(
                {"type": "function_call_output", "call_id": call["call_id"], "output": result}
            )
    scenario.error = {"status": 0, "code": "max_turns_exceeded"}
    return scenario


def responses_ttft(
    client: httpx.Client, token: str, model: str, runs: int, rl: dict[str, str]
) -> dict[str, Any]:
    samples: list[StreamOutcome] = []
    errors: list[dict[str, Any]] = []
    for _ in range(runs):
        body = responses_body(model, [{"role": "user", "content": TTFT_PROMPT}], False)
        out = responses_call(client, token, body, rl)
        if out.http_error or out.failed_code or not out.completed:
            errors.append(out.http_error or {"status": out.status, "code": out.failed_code})
            continue
        samples.append(out)
    return ttft_summary(samples, errors)


def ttft_summary(samples: list[StreamOutcome], errors: list[dict[str, Any]]) -> dict[str, Any]:
    def stats(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"n": 0}
        return {
            "n": len(values),
            "median": round(statistics.median(values), 1),
            "min": round(min(values), 1),
            "max": round(max(values), 1),
            "all": [round(v, 1) for v in values],
        }

    return {
        "runs_ok": len(samples),
        "errors": errors,
        "first_event_ms": stats(
            [s.first_event_ms for s in samples if s.first_event_ms is not None]
        ),
        "first_token_ms": stats(
            [s.first_token_ms for s in samples if s.first_token_ms is not None]
        ),
        "first_content_ms": stats(
            [s.first_content_ms for s in samples if s.first_content_ms is not None]
        ),
        "reasoning_chars_seen": any(s.reasoning_chars for s in samples),
        "usage_samples": [s.usage for s in samples][:3],
    }


def list_models(client: httpx.Client, token: str, rl: dict[str, str]) -> dict[str, Any]:
    response = client.get(f"{OPENAI_API}/models", headers={"Authorization": f"Bearer {token}"})
    _headers_merge(rl, rate_headers(response.headers))
    if response.status_code >= 400:
        return {"ok": False, "error": parse_error(response.status_code, response.text)}
    payload = response.json()
    entries = payload.get("models") or payload.get("data") or []
    listed: list[str] = []
    hidden = 0
    for entry in entries:
        ident = entry.get("slug") or entry.get("id")
        if not ident:
            continue
        if entry.get("visibility", "list") == "list":
            listed.append(str(ident))
        else:
            hidden += 1
    return {"ok": True, "listed": listed, "hidden_count": hidden, "shape": sorted(payload)}


# ----------------------------------------------------------------- OAuth/SIWC


@dataclass
class Session:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    id_token: str = field(repr=False)
    client_id: str = field(repr=False)
    expires_in: int = 0
    scopes: list[str] = field(default_factory=list)


class CallbackServer:
    """Loopback redirect listener on 127.0.0.1 (never localhost, per the SIWC docs)."""

    def __init__(self, preferred_port: int = DEFAULT_PORT) -> None:
        self.params: dict[str, str] = {}
        self.done = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 5  # idle/preconnect sockets must not pin a handler forever

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                ok = parts.path == "/auth/callback"
                if ok:
                    outer.params = {k: v[0] for k, v in parse_qs(parts.query).items()}
                self.send_response(200 if ok else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h3>MIA spike: you can close this tab.</h3>" if ok else b"")
                if ok:
                    outer.done.set()

            def log_message(self, *_args: Any) -> None:  # request lines contain the code
                return

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", preferred_port), Handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True  # per-connection threads never block exit or close
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/auth/callback"

    def start(self) -> None:
        self.thread.start()

    def wait(self, timeout: float) -> dict[str, str]:
        if not self.done.wait(timeout):
            raise TimeoutError("no OAuth callback received")
        return self.params

    def stop(self) -> None:
        # shutdown() waits for serve_forever to leave; never let it block the run.
        closer = threading.Thread(target=self.httpd.shutdown, daemon=True)
        closer.start()
        closer.join(2.0)
        self.httpd.server_close()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_authorize_url(
    redirect_uri: str, state: str, nonce: str, challenge: str, host_id: str
) -> str:
    params = {
        "client_id": DYNAMIC_CLIENT,
        "agent_name_hint": AGENT_NAME,
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": OPENAI_API,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


def jwt_claims_unverified(token: str) -> dict[str, Any]:
    """Decode WITHOUT verifying. M11 must verify the signature against the published JWKS."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return decoded if isinstance(decoded, dict) else {}
    except (IndexError, ValueError):
        return {}


def _token_post(client: httpx.Client, form: dict[str, str]) -> dict[str, Any]:
    response = client.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
    if response.status_code != 200:
        err = parse_error(response.status_code, response.text)
        raise RuntimeError(f"token endpoint failed: {json.dumps(err)}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("token endpoint returned a non-object")
    return payload


def sign_in(
    client: httpx.Client,
    open_browser: Callable[[str], object],
    timeout: float,
    host_id: str,
    port: int,
    checks: dict[str, Any],
) -> Session:
    verifier, challenge = pkce_pair()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    server = CallbackServer(port)
    server.start()
    try:
        checks["redirect_host"] = "127.0.0.1"
        checks["redirect_port_was_default"] = server.port == DEFAULT_PORT
        url = build_authorize_url(server.redirect_uri, state, nonce, challenge, host_id)
        print(f"Opening the browser for Sign in with ChatGPT. If nothing opens, visit:\n{url}\n")
        open_browser(url)
        say(f"waiting for the browser callback (up to {int(timeout)}s)")
        params = server.wait(timeout)
        say("callback received")
    finally:
        server.stop()
    checks["callback_state_ok"] = secrets.compare_digest(params.get("state", ""), state)
    if params.get("error") or not checks["callback_state_ok"] or "code" not in params:
        raise RuntimeError(f"authorization failed: {params.get('error', 'bad state or no code')}")
    client_id = params.get("client_id", "")
    checks["issued_client_id_returned"] = client_id.startswith("oaiapp_")
    if not client_id or client_id == DYNAMIC_CLIENT:
        raise RuntimeError("callback did not return an issued client_id")
    say("exchanging the code for tokens")
    payload = _token_post(
        client,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": params["code"],
            "code_verifier": verifier,
            "redirect_uri": server.redirect_uri,
            "resource": OPENAI_API,
        },
    )
    claims = jwt_claims_unverified(str(payload.get("id_token", "")))
    aud = claims.get("aud")
    checks["id_token_nonce_ok"] = claims.get("nonce") == nonce
    checks["id_token_aud_is_client"] = aud == client_id or (
        isinstance(aud, list) and client_id in aud
    )
    checks["id_token_issuer_ok"] = claims.get("iss") == ISSUER
    checks["id_token_signature_verified"] = False  # spike limitation; M11 must verify via JWKS
    scopes = str(payload.get("scope", "")).split()
    checks["granted_scopes"] = scopes
    checks["direct_scope_granted"] = DIRECT_SCOPE in scopes
    checks["refresh_token_returned"] = bool(payload.get("refresh_token"))
    checks["expires_in"] = payload.get("expires_in")
    checks["token_type"] = payload.get("token_type")
    checks["earliest_refresh_at_present"] = "earliest_refresh_at" in payload
    say(f"token exchanged (direct scope granted: {checks['direct_scope_granted']})")
    return Session(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token", "")),
        id_token=str(payload.get("id_token", "")),
        client_id=client_id,
        expires_in=int(payload.get("expires_in") or 0),
        scopes=scopes,
    )


def refresh_session(client: httpx.Client, session: Session, checks: dict[str, Any]) -> Session:
    payload = _token_post(
        client,
        {
            "grant_type": "refresh_token",
            "client_id": session.client_id,
            "refresh_token": session.refresh_token,
            "resource": OPENAI_API,
        },
    )
    new_refresh = str(payload.get("refresh_token", ""))
    checks["refresh_ok"] = True
    checks["refresh_token_rotated"] = bool(new_refresh) and new_refresh != session.refresh_token
    checks["refresh_expires_in"] = payload.get("expires_in")
    checks["refresh_scopes_retained"] = DIRECT_SCOPE in str(payload.get("scope", ""))
    say(f"token refreshed (rotated: {checks['refresh_token_rotated']})")
    return Session(
        access_token=str(payload["access_token"]),
        refresh_token=new_refresh or session.refresh_token,
        id_token=session.id_token,
        client_id=session.client_id,
        expires_in=int(payload.get("expires_in") or 0),
        scopes=str(payload.get("scope", "")).split() or session.scopes,
    )


def revoke_session(client: httpx.Client, session: Session) -> bool:
    response = client.post(
        REVOKE_URL,
        data={
            "token": session.refresh_token,
            "token_type_hint": "refresh_token",
            "client_id": session.client_id,
        },
    )
    return response.status_code == 200


# ------------------------------------------------------------------ providers


def new_result(provider: str, dry_run: bool) -> dict[str, Any]:
    return {
        "provider": provider,
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": dry_run,
        "steps": {},
    }


def step(container: dict[str, Any], name: str, fn: Callable[[], Any]) -> Any:
    """Run one spike step; a failure is recorded (type + sanitized message), not fatal."""
    try:
        value = fn()
        container[name] = value
        return value
    except Exception as exc:  # noqa: BLE001 - spike records every failure mode
        container[name] = {
            "ok": False,
            "exception": type(exc).__name__,
            "message": redact_text(str(exc))[:300],
        }
        return None


def run_gpt_plan(
    client: httpx.Client,
    args: argparse.Namespace,
    open_browser: Callable[[str], object],
    live_secrets: list[str],
) -> dict[str, Any]:
    result = new_result("gpt-plan", args.dry_run)
    rl: dict[str, str] = {}
    checks: dict[str, Any] = {}
    steps: dict[str, Any] = result["steps"]
    steps["oauth"] = checks

    def discovery() -> dict[str, Any]:
        meta = client.get(DISCOVERY_URL).json()
        keys = ("issuer", "authorization_endpoint", "token_endpoint", "revocation_endpoint")
        return {
            "ok": True,
            **{k: meta.get(k) for k in keys},
            "scopes_supported": meta.get("scopes_supported"),
            "code_challenge_methods_supported": meta.get("code_challenge_methods_supported"),
        }

    step(steps, "discovery", discovery)
    host_id = f"urn:uuid:{uuid.uuid4()}"
    scratch: dict[str, Any] = {}  # holds Session objects: never serialized
    session = step(
        scratch,
        "oauth_sign_in",
        lambda: sign_in(client, open_browser, args.login_timeout, host_id, args.port, checks),
    )
    if session is None:
        steps["oauth_sign_in"] = scratch["oauth_sign_in"]
        return result
    steps["oauth_sign_in"] = {"ok": True}
    live_secrets += [session.access_token, session.refresh_token, session.id_token]
    refreshed = step(scratch, "oauth_refresh", lambda: refresh_session(client, session, checks))
    if refreshed is None:
        steps["oauth_refresh"] = scratch["oauth_refresh"]
    else:
        live_secrets += [refreshed.access_token, refreshed.refresh_token]
        steps["oauth_refresh"] = {"ok": True}
        session = refreshed
    token = session.access_token

    models = step(steps, "models", lambda: list_models(client, token, rl))
    say(f"models listed: {len((models or {}).get('listed') or [])}")
    chosen = args.model or ((models or {}).get("listed") or [])[:1]
    result["models_tested"] = chosen
    for model in chosen:
        per_model: dict[str, Any] = {}
        steps[f"model:{model}"] = per_model
        say(f"stream started: {model}, {args.runs} TTFT runs")
        step(per_model, "ttft", lambda m=model: responses_ttft(client, token, m, args.runs, rl))

        def tools(m: str = model) -> dict[str, Any]:
            first = responses_tool_loop(client, token, m, "plain", rl)
            if first.error and first.error.get("status") == 400:
                second = responses_tool_loop(client, token, m, "encrypted", rl)
                summary = second.finalize()
                summary["plain_mode_error"] = first.error
                return summary
            return first.finalize()

        say(f"tool scenario: {model}")
        step(per_model, "tool_calling", tools)
    result["rate_limit_headers"] = rl
    if not args.no_revoke:
        step(steps, "revoke", lambda: {"ok": revoke_session(client, session)})
    return result


def run_glm(client: httpx.Client, args: argparse.Namespace, api_key: str) -> dict[str, Any]:
    result = new_result("glm", args.dry_run)
    rl: dict[str, str] = {}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{DEEPINFRA_API}/chat/completions"

    def call(messages: list[dict[str, Any]], tools: bool) -> StreamOutcome:
        out = StreamOutcome()
        acc = ChatAccumulator(out)
        body: dict[str, Any] = {
            "model": args.glm_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = [{"type": "function", "function": spec} for spec in TOOL_SPECS]
            body["tool_choice"] = "auto"
        stream_request(client, url, headers, body, acc.feed, out)
        _headers_merge(rl, out.headers)
        return out

    def catalog() -> dict[str, Any]:
        entries = client.get(f"{DEEPINFRA_API}/models").json().get("data", [])
        meta = next((m for m in entries if m.get("id") == args.glm_model), None)
        if meta is None:
            return {"ok": True, "model_listed": False}
        info = meta.get("metadata") or {}
        return {
            "ok": True,
            "model_listed": True,
            "context_length": info.get("context_length"),
            "pricing_usd_per_mtok": info.get("pricing"),
            "tags": info.get("tags"),
        }

    def ttft() -> dict[str, Any]:
        samples, errors = [], []
        for _ in range(args.runs):
            out = call([{"role": "user", "content": TTFT_PROMPT}], False)
            if out.http_error or not out.completed:
                errors.append(out.http_error or {"status": out.status, "code": None})
            else:
                samples.append(out)
        return ttft_summary(samples, errors)

    def tools() -> dict[str, Any]:
        scenario = ToolScenario(continuation_mode="reasoning_content_replayed")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": TOOL_PROMPT},
        ]
        for _ in range(MAX_TURNS):
            out = call(messages, True)
            scenario.record_turn(out)
            say(f"tool turn {len(scenario.turns)}: {[c['name'] for c in out.tool_calls] or 'text'}")
            if out.http_error or not out.completed:
                scenario.error = out.http_error or {"status": out.status, "code": None}
                return scenario.finalize()
            if not out.tool_calls:
                scenario.final_text_nonempty = bool(out.text.strip())
                return scenario.finalize()
            messages.append(out.assistant_message)  # keeps reasoning_content for continuation
            for c in out.tool_calls:
                output, valid = execute_tool(c["name"], c["arguments"])
                scenario.tools_called.append(c["name"])
                scenario.args_valid = scenario.args_valid and valid
                messages.append({"role": "tool", "tool_call_id": c["call_id"], "content": output})
        scenario.error = {"status": 0, "code": "max_turns_exceeded"}
        return scenario.finalize()

    result["model"] = args.glm_model
    say(f"glm: {args.glm_model}")
    step(result["steps"], "catalog", catalog)
    say(f"glm: stream started, {args.runs} TTFT runs")
    step(result["steps"], "ttft", ttft)
    say("glm: tool scenario")
    step(result["steps"], "tool_calling", tools)
    result["rate_limit_headers"] = rl
    return result


# ----------------------------------------------------------------------- main


def write_result(result: dict[str, Any], out_dir: Path, live_secrets: list[str]) -> Path:
    clean = sanitize(result)
    text = json.dumps(clean, indent=2, ensure_ascii=False, sort_keys=True)
    assert_clean(text, live_secrets)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{result['provider']}-{stamp}{'-dry' if result['dry_run'] else ''}.json"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def summarize(result: dict[str, Any]) -> str:
    lines = [f"== {result['provider']}{' (dry-run)' if result['dry_run'] else ''} =="]
    steps = result["steps"]
    blocks = (
        {k[6:]: v for k, v in steps.items() if k.startswith("model:")}
        if result["provider"] == "gpt-plan"
        else {result.get("model", "?"): steps}
    )
    if result["provider"] == "gpt-plan":
        oauth = steps.get("oauth", {})
        lines.append(
            f"oauth: direct_scope={oauth.get('direct_scope_granted')} "
            f"refresh_ok={oauth.get('refresh_ok')} rotated={oauth.get('refresh_token_rotated')}"
        )
        lines.append(f"models listed: {(steps.get('models') or {}).get('listed')}")
    for model, block in blocks.items():
        ttft = block.get("ttft") or {}
        tool = block.get("tool_calling") or {}
        lines.append(
            f"{model}: first_token_ms median={(ttft.get('first_token_ms') or {}).get('median')} "
            f"tools_ok={tool.get('ok')} tools={tool.get('tools_called')} "
            f"reasoning={tool.get('reasoning_seen')}"
        )
    lines.append(f"rate-limit headers: {sorted(result.get('rate_limit_headers', {}))}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--provider", choices=["gpt-plan", "glm", "all"], default="all")
    p.add_argument("--runs", type=int, default=5, help="TTFT samples per model (default 5)")
    p.add_argument("--model", action="append", help="GPT model slug (repeatable); default: first")
    p.add_argument("--glm-model", default=GLM_MODEL)
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="loopback callback port")
    p.add_argument("--login-timeout", type=float, default=300.0)
    p.add_argument("--no-revoke", action="store_true", help="keep the SIWC grant after the run")
    p.add_argument("--dry-run", action="store_true", help="fakes only: no network/browser/secrets")
    p.add_argument("--out-dir", type=Path, default=Path(__file__).parent / "results")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    providers = ["gpt-plan", "glm"] if args.provider == "all" else [args.provider]
    live_secrets: list[str] = []
    failed = False
    for provider in providers:
        if args.dry_run:
            sys.path.insert(0, str(Path(__file__).parent))
            import fakes  # noqa: PLC0415

            world = fakes.FakeWorld()
            client = httpx.Client(transport=httpx.MockTransport(world.handler), timeout=30)
            opener: Callable[[str], object] = world.open_browser
            key = "fake-deepinfra-key-for-dry-run"
        else:
            client = httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
            opener = webbrowser.open
            key = ""
        with client:
            if provider == "glm":
                if not key:
                    key = os.environ.get("DEEPINFRA_API_KEY") or getpass.getpass(
                        "DEEPINFRA_API_KEY (input hidden): "
                    )
                live_secrets.append(key)
                result = run_glm(client, args, key)
            else:
                result = run_gpt_plan(client, args, opener, live_secrets)
        path = write_result(result, args.out_dir, live_secrets)
        say(f"{provider}: done")
        print(summarize(sanitize(result)))
        print(f"results: {path}\n")
        failed = failed or not any(
            isinstance(v, dict) and v.get("ok") for v in _flatten_tool_blocks(result)
        )
    return 1 if failed else 0


def _flatten_tool_blocks(result: dict[str, Any]) -> Iterator[Any]:
    steps = result["steps"]
    for key, value in steps.items():
        if key == "tool_calling":
            yield value
        elif key.startswith("model:"):
            yield value.get("tool_calling")


if __name__ == "__main__":
    raise SystemExit(main())
