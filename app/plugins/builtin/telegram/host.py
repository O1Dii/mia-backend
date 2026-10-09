"""What the Telegram plugin needs from Mia (people, the run's phone, the bot link).

The in-process server cannot import run state, so the composition binds one
:class:`TelegramHost` (``bind_host``). Tests bind a fake; without a host every tool answers a
clear error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.modules.plugins.builtin.identity import CallIdentity


@dataclass(frozen=True, slots=True)
class Recipient:
    ref: str
    """``person:<id>`` (memory) or ``contact:<n>`` (phone contact of this run)."""
    name: str
    username: str | None = None
    """Telegram username when memory knows it; otherwise the phone resolves ``name``."""
    relation: str | None = None


@dataclass(frozen=True, slots=True)
class HandoffOutcome:
    status: Literal["opened", "failed", "uncertain"]
    message: str
    """User-safe explanation (Russian); never contains the message text."""
    direct_chat: bool = False


@dataclass(frozen=True, slots=True)
class BotLinkOffer:
    already_linked: bool
    deep_link: str | None = None
    expires_at: str | None = None


class HostUnavailableError(RuntimeError):
    """The host cannot do this now (no phone for the run, bot not configured); user-safe text."""


class TelegramHost(Protocol):
    async def candidates(self, identity: CallIdentity, to: str) -> list[Recipient]:
        """Resolve ``to`` (a ``person:``/``contact:`` ref or a name); empty when nobody matches."""
        ...

    async def hand_off(
        self, identity: CallIdentity, recipient: Recipient, text: str
    ) -> HandoffOutcome:
        """Run the ``telegram.handoff`` device tool on the phone of the calling run (L0)."""
        ...

    async def bot_link(self, identity: CallIdentity) -> BotLinkOffer: ...


_host: TelegramHost | None = None


def bind_host(host: TelegramHost | None) -> None:
    global _host
    _host = host


def current_host() -> TelegramHost | None:
    return _host
