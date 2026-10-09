"""Helpers for plugin server tests: SDK client over the in-memory transport and fakes."""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from app.modules.plugins.builtin import (
    BuiltinManifestSource,
    BuiltinPlugin,
    CallIdentity,
    HostLlmError,
    HostServices,
    InMemoryPluginStateStore,
    LlmReply,
    PluginStateStore,
    make_inprocess_factory,
)
from app.modules.plugins.builtin.web import PublicWebError
from mcp import Client
from mcp_types import CallToolResult, RequestParamsMeta

ROOT = Path(__file__).resolve().parents[2]
BASE_TIME = 1_800_000_000.0


class FakeClock:
    def __init__(self, now: float = BASE_TIME) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWeb:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items if items is not None else []
        self.calls: list[tuple[str, str, bool]] = []
        self.fail: str | None = None

    async def search(self, query: str, *, news: bool = False) -> list[dict[str, Any]]:
        self.calls.append(("search", query, news))
        if self.fail:
            raise PublicWebError(self.fail)
        return list(self.items)

    async def read(self, url: str) -> dict[str, str]:
        self.calls.append(("read", url, False))
        if self.fail:
            raise PublicWebError(self.fail)
        return {"url": url, "text": "Текст страницы", "truncated": "False"}


class FakeLlm:
    def __init__(self, reply: str = "Сводка [1]") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.identities: list[CallIdentity] = []
        self.fail: str | None = None

    async def generate(self, prompt: str, *, identity: CallIdentity, plugin_id: str) -> LlmReply:
        self.prompts.append(prompt)
        self.identities.append(identity)
        if self.fail:
            raise HostLlmError(self.fail)
        return LlmReply(text=self.reply, model="fake-model")


def make_services(
    clock: FakeClock | None = None,
    *,
    web: FakeWeb | None = None,
    llm: FakeLlm | None = None,
    state: PluginStateStore | None = None,
) -> HostServices:
    counter = itertools.count(1)
    return HostServices(
        state=state or InMemoryPluginStateStore(),
        clock=clock or FakeClock(),
        new_id=lambda: f"id{next(counter)}",
        web=web,
        llm=llm,
    )


def as_meta(values: Mapping[str, Any]) -> RequestParamsMeta:
    """A request ``_meta`` object (an open map) from plain keys."""
    return cast(RequestParamsMeta, dict(values))


def meta(
    installation: str = "inst-a", workspace: str = "ws-1", user: str = "user-1"
) -> RequestParamsMeta:
    return as_meta(
        {
            "mia/user_id": user,
            "mia/workspace_id": workspace,
            "mia/installation_id": installation,
            "mia/run_id": "run-1",
        }
    )


def builtin(plugin_id: str) -> BuiltinPlugin:
    plugin = BuiltinManifestSource().get(plugin_id)
    assert plugin is not None, plugin_id
    return plugin


def server_for(plugin_id: str, services: HostServices) -> Any:
    plugin = builtin(plugin_id)
    return make_inprocess_factory(services)(plugin.manifest, plugin.directory)


@asynccontextmanager
async def connect(server: Any, mode: str = "auto") -> AsyncIterator[Client]:
    async with Client(server, mode=mode) as client:
        yield client


class Caller:
    """Bound ``call_tool`` with a fixed identity; raises on tool errors unless asked."""

    def __init__(self, client: Client, identity: RequestParamsMeta) -> None:
        self.client = client
        self.identity = identity

    async def raw(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        return await self.client.call_tool(name, arguments or {}, meta=self.identity)

    async def __call__(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self.raw(name, arguments)
        assert not result.is_error, [c.model_dump() for c in result.content]
        assert result.structured_content is not None
        return dict(result.structured_content)

    async def text(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        result = await self.raw(name, arguments)
        assert not result.is_error
        return "".join(getattr(part, "text", "") for part in result.content)

    async def error(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        result = await self.raw(name, arguments)
        assert result.is_error, "expected a tool error"
        return "".join(getattr(part, "text", "") for part in result.content)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


Factory = Callable[[Any, Path], Any]
