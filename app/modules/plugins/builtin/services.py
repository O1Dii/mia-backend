"""Host services injected into in-process plugin servers (state, clock, web, model)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.modules.plugins.builtin.identity import CallIdentity
from app.modules.plugins.builtin.state import InMemoryPluginStateStore, PluginStateStore


@dataclass(frozen=True, slots=True)
class LlmReply:
    text: str
    model: str


class HostLlmError(Exception):
    """A user-safe model failure (no model selected, budget exceeded, provider error)."""


class HostLlm(Protocol):
    """The user's model through Mia (``permissions.llm.viaHost``).

    The integrator implements it over M11's ``ModelGateway`` with the budget of the
    installation; plugins never see provider keys.
    """

    async def generate(
        self, prompt: str, *, identity: CallIdentity, plugin_id: str
    ) -> LlmReply: ...


class PublicWeb(Protocol):
    """Public-web access (``PublicWebClient`` in production, a fake in tests)."""

    async def search(self, query: str, *, news: bool = False) -> list[dict[str, Any]]: ...

    async def read(self, url: str) -> dict[str, str]: ...


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass(frozen=True)
class HostServices:
    state: PluginStateStore = field(default_factory=InMemoryPluginStateStore)
    clock: Callable[[], float] = time.time
    """Seconds since the epoch; tests inject a controllable clock."""
    new_id: Callable[[], str] = _new_id
    web: PublicWeb | None = None
    llm: HostLlm | None = None

    def now_ms(self) -> int:
        return int(self.clock() * 1000)
