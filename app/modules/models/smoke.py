"""Manual live smoke for the model adapters. Credentials come from the environment or a browser.

    uv run python -m app.modules.models.smoke --provider gpt-plan      # interactive sign-in
    DEEPINFRA_API_KEY=... uv run python -m app.modules.models.smoke --provider glm
    ... --record tests/models/cassettes   # also write scrubbed record/replay cassettes

``gpt-plan`` opens the browser for Sign in with ChatGPT (loopback ``127.0.0.1``), verifies the
ID token against the issuer's JWKS, then runs the M01 tool scenario through the *product*
adapter. Tokens and keys live in memory only and are never printed. Exit code 0 means every
selected provider passed; a provider without credentials is reported as ``not run``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
import time
import uuid
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    TextDelta,
    ToolCallRequested,
    ToolSpec,
)
from app.modules.models.adapters import AdapterSettings, AdapterTarget, TokenSource, build_adapter
from app.modules.models.catalog import FALLBACK_MODEL, PROVIDERS
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.chatgpt_plan.jwks import JwksVerifier
from app.modules.models.errors import ProviderError

PROMPT = (
    "First call get_time for timezone UTC. Then call add_todo with the title 'Call Anna' and "
    "the due time one hour after the time you got. Finally confirm in one short sentence."
)
TOOLS = (
    ToolSpec(
        "get_time",
        "Return the current time in an IANA timezone.",
        {
            "type": "object",
            "properties": {"timezone": {"type": "string"}},
            "required": ["timezone"],
        },
    ),
    ToolSpec(
        "add_todo",
        "Add a todo item.",
        {
            "type": "object",
            "properties": {"title": {"type": "string"}, "due": {"type": "string"}},
            "required": ["title"],
        },
    ),
)
_SECRET_PATTERNS = (
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bBearer\s+\S+", re.I),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
)


def say(message: str) -> None:
    print(f"[m11-smoke] {message}", flush=True)


def scrub(value: Any) -> Any:
    """Redact secret-looking strings and long opaque blobs from a recorded payload."""
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub("[scrubbed]", value)
        return "[scrubbed]" if re.fullmatch(r"[A-Za-z0-9+/_=-]{80,}", value) else value
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


class RecordingTransport(httpx.AsyncBaseTransport):
    """Wrap a transport and keep the model API exchanges as replay-format interactions."""

    def __init__(self, inner: httpx.AsyncBaseTransport, hosts: set[str]) -> None:
        self._inner = inner
        self._hosts = hosts
        self.interactions: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.url.host not in self._hosts or request.method != "POST":
            return response
        body = b"".join([chunk async for chunk in response.aiter_raw()])
        await response.aclose()
        self.interactions.append(self._interaction(request, response, body))
        return httpx.Response(
            response.status_code, headers=response.headers, content=body, request=request
        )

    @staticmethod
    def _interaction(
        request: httpx.Request, response: httpx.Response, body: bytes
    ) -> dict[str, Any]:
        text = body.decode("utf-8", errors="replace")
        spec: dict[str, Any] = {"status": response.status_code, "headers": {}}
        if "text/event-stream" in response.headers.get("content-type", ""):
            events: list[dict[str, Any]] = []
            name: str | None = None
            for line in text.splitlines():
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        data: Any = scrub(json.loads(raw))
                    except ValueError:
                        data = raw
                    events.append({"event": name, "data": data})
                    name = None
            spec["events"] = events
        else:
            try:
                spec["json"] = scrub(json.loads(text))
            except ValueError:
                spec["text"] = scrub(text)[:500]
        return {
            "request": {"method": request.method, "url": str(request.url), "forbid_body_keys": []},
            "response": spec,
        }

    async def aclose(self) -> None:
        await self._inner.aclose()


class MemoryTokens:
    """In-memory ``TokenSource`` with serialized refresh (the smoke has no database)."""

    def __init__(self, client: httpx.AsyncClient, tokens: oauth.TokenSet) -> None:
        self._client = client
        self._tokens = tokens
        self._lock = asyncio.Lock()
        self._rejected: str | None = None

    async def access_token(self) -> str:
        async with self._lock:
            if self._tokens.fresh(time.time()) and self._tokens.access_token != self._rejected:
                return self._tokens.access_token
            self._tokens = await oauth.refresh_tokens(self._client, self._tokens)
            return self._tokens.access_token

    async def invalidate(self) -> None:
        self._rejected = self._tokens.access_token

    @property
    def tokens(self) -> oauth.TokenSet:
        return self._tokens


async def sign_in(
    client: httpx.AsyncClient,
    open_browser: Callable[[str], object],
    timeout_seconds: float,
    port: int,
) -> oauth.TokenSet:
    verifier, challenge = oauth.pkce_pair()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    server = oauth.CallbackServer(port)
    server.start()
    try:
        url = oauth.build_authorize_url(
            redirect_uri=server.redirect_uri,
            state=state,
            nonce=nonce,
            challenge=challenge,
            host_id=f"urn:uuid:{uuid.uuid4()}",
        )
        print(f"Opening the browser for Sign in with ChatGPT. If nothing opens, visit:\n{url}\n")
        open_browser(url)
        say(f"waiting for the browser callback (up to {int(timeout_seconds)}s)")
        params = await server.wait(timeout_seconds)
    finally:
        await server.stop()
    if params.get("error") or not secrets.compare_digest(params.get("state", ""), state):
        raise RuntimeError("authorization failed (error or state mismatch)")
    client_id = params.get("client_id", "")
    if not client_id.startswith("oaiapp_") or "code" not in params:
        raise RuntimeError("callback did not return an issued client id and code")
    tokens, id_token = await oauth.exchange_code(
        client,
        client_id=client_id,
        code=params["code"],
        verifier=verifier,
        redirect_uri=server.redirect_uri,
    )
    if oauth.DIRECT_SCOPE not in tokens.scopes:
        raise RuntimeError("the direct-use scope was not granted")
    await JwksVerifier(client, issuer=oauth.ISSUER).verify(
        id_token, audience=client_id, nonce=nonce
    )
    say("signed in; ID token signature verified via JWKS")
    return tokens


async def list_plan_models(client: httpx.AsyncClient, source: TokenSource) -> list[str]:
    response = await client.get(
        f"{oauth.RESOURCE}/models",
        headers={"Authorization": f"Bearer {await source.access_token()}"},
    )
    response.raise_for_status()
    entries = response.json().get("models") or []
    return [
        str(e["slug"]) for e in entries if e.get("visibility", "list") == "list" and e.get("slug")
    ]


def run_tool(name: str, arguments: dict[str, object]) -> str:
    if name == "get_time":
        return json.dumps({"timezone": arguments.get("timezone"), "now": "2026-01-01T12:00:00Z"})
    if name == "add_todo":
        return json.dumps({"id": 1, "status": "added", "title": arguments.get("title")})
    return json.dumps({"error": "unknown tool"})


async def tool_scenario(
    client: httpx.AsyncClient, target: AdapterTarget, label: str
) -> dict[str, Any]:
    """The M01 scenario through the product adapter; returns a content-free summary."""
    adapter = build_adapter(client, target, AdapterSettings())
    messages: list[ModelMessage] = [ModelMessage("user", PROMPT)]
    summary: dict[str, Any] = {"provider": label, "tools": [], "turns": 0, "ok": False}
    for _ in range(5):
        request = ModelRequest(
            user_id=str(uuid.uuid4()), profile="chat", messages=messages, tools=TOOLS
        )
        started = time.perf_counter()
        events: list[ModelEvent] = []
        try:
            async for event in adapter.stream(request):
                if not events:
                    summary.setdefault(
                        "first_event_ms", round((time.perf_counter() - started) * 1000)
                    )
                events.append(event)
        except ProviderError as error:
            summary["error"] = error.code
            summary["provider_code"] = error.provider_code
            return summary
        summary["turns"] += 1
        calls: list[ModelToolCall] = [e.call for e in events if isinstance(e, ToolCallRequested)]
        if not calls:
            text = "".join(e.text for e in events if isinstance(e, TextDelta))
            summary["ok"] = (
                bool(text.strip())
                and {"get_time", "add_todo"} <= set(summary["tools"])
                and isinstance(events[-1], ModelDone)
            )
            return summary
        for call in calls:
            messages.append(ModelMessage("assistant", "", tool_calls=(call,)))
            messages.append(
                ModelMessage(
                    "tool", run_tool(call.name, dict(call.arguments)), tool_call_id=call.call_id
                )
            )
            summary["tools"].append(call.name)
    summary["error"] = "max_turns_exceeded"
    return summary


def write_cassette(directory: Path, name: str, recorder: RecordingTransport) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    payload = {
        "name": name,
        "note": "Recorded live; scrubbed.",
        "interactions": recorder.interactions,
    }
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    say(f"wrote {path} ({len(recorder.interactions)} interactions)")


async def run_smoke(
    providers: list[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
    model: str | None = None,
    login_timeout: float = 300.0,
    port: int = oauth.DEFAULT_PORT,
    record_dir: Path | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    env = dict(os.environ) if environ is None else environ
    failures = 0
    for provider in providers:
        recorder: RecordingTransport | None = None
        base = transport or httpx.AsyncHTTPTransport()
        if record_dir is not None:
            hosts = {"api.openai.com"} if provider == "gpt-plan" else {"api.deepinfra.com"}
            recorder = RecordingTransport(base, hosts)
        client = httpx.AsyncClient(
            transport=recorder or base, timeout=httpx.Timeout(60.0, connect=5.0), trust_env=False
        )
        try:
            if provider == "gpt-plan":
                tokens = await sign_in(client, open_browser, login_timeout, port)
                source = MemoryTokens(client, tokens)
                chosen = model or (await list_plan_models(client, source) or ["gpt-6.1-sol"])[0]
                target = AdapterTarget(
                    PROVIDERS["chatgpt-plan"],
                    chosen,
                    PROVIDERS["chatgpt-plan"].default_url,
                    tokens=source,
                )
                result = await tool_scenario(client, target, f"chatgpt-plan/{chosen}")
                await oauth.revoke_refresh_token(client, source.tokens)
            else:
                key = env.get("DEEPINFRA_API_KEY")
                if not key:
                    say("glm: NOT RUN (DEEPINFRA_API_KEY is not set in the environment)")
                    continue
                spec = PROVIDERS["deepinfra"]
                target = AdapterTarget(
                    spec, model or FALLBACK_MODEL, spec.default_url, api_key=SecretStr(key)
                )
                result = await tool_scenario(client, target, f"deepinfra/{target.model}")
        except (RuntimeError, TimeoutError, httpx.HTTPError, oauth.TokenEndpointError) as exc:
            say(f"{provider}: FAIL ({type(exc).__name__})")
            failures += 1
            continue
        finally:
            await client.aclose()
        say(
            f"{provider}: {'PASS' if result['ok'] else 'FAIL'} {json.dumps(result, sort_keys=True)}"
        )
        failures += 0 if result["ok"] else 1
        if recorder is not None and record_dir is not None and result["ok"]:
            write_cassette(record_dir, f"live_{provider.replace('-', '_')}_tool_loop", recorder)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--provider", choices=["gpt-plan", "glm", "all"], default="all")
    parser.add_argument("--model", help="override the model slug")
    parser.add_argument("--port", type=int, default=oauth.DEFAULT_PORT)
    parser.add_argument("--login-timeout", type=float, default=300.0)
    parser.add_argument("--record", type=Path, help="write scrubbed replay cassettes here")
    args = parser.parse_args(argv)
    providers = ["gpt-plan", "glm"] if args.provider == "all" else [args.provider]
    return asyncio.run(
        run_smoke(
            providers,
            model=args.model,
            login_timeout=args.login_timeout,
            port=args.port,
            record_dir=args.record,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
