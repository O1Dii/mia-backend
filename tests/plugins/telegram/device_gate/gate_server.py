"""M41 device-gate backend: «Напиши Яне Гейтовой в телеграм, что надо купить молоко».

Run by ``run_gate.sh`` from a mia-helper checkout containing M41 (``PYTHONPATH=.``). It is the REAL
application (auth, run engine, tool router with the in-process device channel, memory tools, the
installed ``builtin.telegram`` plugin, approvals). Only two things are replaced:

- the model: a script that turns the spoken request into ``people.resolve`` -> ``send_message``
  (with the candidate's ref) and then speaks what the tool reported;
- the human tap on «Подтвердить»: a background task approves the pending approval over its exact
  arguments (the real approval round trip still happens; the on-screen tap is a user check).

The phone (Android instrumentation ``DeviceToolsVoiceGateTest#findsAContactByNameByVoice`` with the
test contact «Яна Гейтова») executes the real ``device.contacts.search`` and ``telegram.handoff``.
Telegram opens with the draft; nothing is ever sent. Prints only non-secret status lines; the bearer
token goes to GATE_TOKEN_FILE (0600).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import threading
import time
import urllib.request
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ["AUTH_JWT_SECRET"] = secrets.token_urlsafe(48)
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("AI_PROVIDER", "fake")

import uvicorn  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.composition as composition  # noqa: E402
from app.core.ports.model_gateway import (  # noqa: E402
    ModelDone,
    ModelEvent,
    ModelInfo,
    ModelRequest,
    ModelToolCall,
    TextDelta,
    ToolCallRequested,
)
from app.core.ports.types import DEFAULT_MODEL  # noqa: E402
from app.modules.auth.models import AuthSession, Device  # noqa: E402
from app.modules.auth.tokens import TokenManager  # noqa: E402
from app.modules.plugins.builtin import BuiltinManifestSource, HostServices  # noqa: E402
from app.modules.plugins.builtin.factory import build_inprocess_server  # noqa: E402
from app.modules.plugins.composition import list_server_tools  # noqa: E402
from app.modules.tools.digest import compute_tools_digest  # noqa: E402
from app.modules.users.models import User  # noqa: E402

PORT = int(os.environ.get("GATE_PORT", "8141"))
TOKEN_FILE = os.environ["GATE_TOKEN_FILE"]
USER_ID = uuid.UUID("41000000-0000-4000-8000-000000000001")
DEVICE_ID = uuid.UUID("41000000-0000-4000-8000-000000000002")
FAMILY_ID = uuid.UUID("41000000-0000-4000-8000-000000000003")
PLUGIN = "builtin.telegram"
SEND = f"{PLUGIN}.send_message"
TEXT = "Надо купить молоко"
APP: object = None


def answer(speech: str, markdown: str) -> str:
    for ch in "*`#|[]<>\r\n":
        speech = speech.replace(ch, " ")
    return json.dumps(
        {
            "speech": speech,
            "display": {"kind": "card", "title": "Telegram (M41 gate)", "markdown": markdown},
            "followups": [],
        },
        ensure_ascii=False,
    )


async def auto_approve() -> None:
    """Approve the pending approval of the gate user over its own args_hash (a user tap on the phone/web)."""
    approvals = APP.state.tools.approvals  # type: ignore[attr-defined]
    for _ in range(300):
        for item in await approvals.list_for_user(user_id=str(USER_ID), status="pending"):
            print(f"gate: approval requested: {item.tool} {item.summary[:120]}", flush=True)
            await approvals.decide(
                user_id=str(USER_ID),
                approval_id=item.id,
                decision="approve",
                args_hash=item.args_hash,
                channel="android",
            )
            print("gate: approval approved", flush=True)
            return
        await asyncio.sleep(0.1)


class GateGateway:
    info = ModelInfo(
        ref="fake/m41-gate",
        provider="fake",
        supports_tools=True,
        supports_structured_output=True,
        verified=True,
    )

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        return self.info

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.profile == "job":  # feed summarizer
            yield TextDelta("Короткая сводка разговора.")
            yield ModelDone("stop")
            return
        messages = list(request.messages)
        last_user = max(i for i, m in enumerate(messages) if m.role == "user")
        text = messages[last_user].content.strip().lower()
        offered = {spec.name for spec in request.tools}
        results = [m for m in messages[last_user + 1 :] if m.role == "tool"]
        print(f"gate: model step {len(results)} for '{text}' (offered {len(offered)} tools)", flush=True)

        def call(name: str, arguments: dict[str, object]) -> list[ModelEvent]:
            print(f"gate: model calls {name}", flush=True)
            return [
                ToolCallRequested(
                    ModelToolCall(call_id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments)
                ),
                ModelDone("tool_calls"),
            ]

        if "телеграм" in text and len(results) == 0 and "people.resolve" in offered:
            for event in call("people.resolve", {"name": "Гейтов"}):
                yield event
            return
        if "телеграм" in text and len(results) == 1:
            body = json.loads(results[-1].content)
            match = re.search(r'"ref": "(contact:\d+)"', str(body.get("content")))
            print(f"gate: people.resolve -> {body.get('status')}: {str(body.get('content'))[:200]}", flush=True)
            if match and SEND in offered:
                asyncio.get_running_loop().create_task(auto_approve())
                for event in call(SEND, {"to": match.group(1), "text": TEXT}):
                    yield event
                return
        if results:
            body = json.loads(results[-1].content)
            ok = body.get("status") == "succeeded"
            print(f"gate: tool result {body.get('status')}: {str(body.get('content'))[:300]}", flush=True)
            speech = (
                "Открыла Telegram с черновиком, нажмите отправить."
                if ok
                else "Не получилось: " + str(body.get("error", {}).get("message", "ошибка"))
            )
            yield TextDelta(answer(speech, f"Инструмент: {str(body.get('content'))[:300]}"))
            yield ModelDone("stop")
            return
        yield TextDelta(answer("Не знаю такой команды.", f"Запрос: {text}"))
        yield ModelDone("stop")


original_install_runs = composition.install_runs


def install_runs(app, settings, runtime, *, authenticator, model_gateway=None, **kwargs):  # type: ignore[no-untyped-def]
    return original_install_runs(
        app, settings, runtime, authenticator=authenticator, model_gateway=GateGateway(), **kwargs
    )


composition.install_runs = install_runs


async def seed_user() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        async with factory() as session, session.begin():
            if await session.get(User, USER_ID) is None:
                session.add(
                    User(
                        id=USER_ID, google_sub="m41-gate-user", email="m41-gate@example.test",
                        email_verified=True, locale="ru", timezone="Europe/Minsk",
                    )
                )
                await session.flush()
                session.add(
                    Device(
                        id=DEVICE_ID, user_id=USER_ID, platform="android", app_version="m41-gate",
                        capabilities={}, last_seen_at=now,
                    )
                )
                await session.flush()
                session.add(
                    AuthSession(
                        id=uuid.uuid4(), family_id=FAMILY_ID, user_id=USER_ID, device_id=DEVICE_ID,
                        refresh_token_hash=secrets.token_hex(32), expires_at=now + timedelta(days=1),
                    )
                )
    finally:
        await engine.dispose()


def telegram_digest() -> tuple[str, str]:
    plugin = BuiltinManifestSource().get(PLUGIN)
    assert plugin is not None
    server = build_inprocess_server(plugin.manifest, Path(plugin.directory), services=HostServices())
    return plugin.version, compute_tools_digest(asyncio.run(list_server_tools(server)))


def install_plugin(token: str) -> None:
    """Install builtin.telegram for the gate user through the public API once the server is up."""
    version, digest = telegram_digest()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health/live", timeout=1).read()
            break
        except OSError:
            time.sleep(0.2)
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/installations",
        data=json.dumps({"plugin_id": PLUGIN, "version": version, "tools_digest": digest}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": f"m41-gate-install-{uuid.uuid4().hex}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(f"gate: installed {PLUGIN} -> HTTP {response.status}", flush=True)
    except Exception as error:  # status line only; the response may hold nothing secret but stay short
        print(f"gate: install failed: {type(error).__name__} {getattr(error, 'code', '')}", flush=True)


def main() -> None:
    global APP
    from app.main import create_app

    asyncio.run(seed_user())
    token, _ = TokenManager(os.environ["AUTH_JWT_SECRET"], access_ttl_seconds=2 * 3600).issue_access(
        USER_ID, FAMILY_ID
    )
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    print(f"gate: token written ({len(token)} chars), serving on 127.0.0.1:{PORT}", flush=True)
    APP = create_app()
    threading.Thread(target=install_plugin, args=(token,), daemon=True).start()
    uvicorn.run(APP, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
