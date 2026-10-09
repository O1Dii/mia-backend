"""Telegram L0: a message to a person through the phone's Telegram (the user presses send).

``send_message`` is ``write_external``: the router asks for an approval over the exact
arguments before this server runs. Here ``to`` is resolved against Mia's memory and the
phone's contacts; several matches or none stop the call before anything reaches the phone.
Telegram's Bot API cannot message other people, so the text is handed to the Telegram app
(``telegram.handoff``) and the tool result says honestly that nothing was sent yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult
from pydantic import Field

from app.modules.plugins.builtin.kit import PluginKit, ok
from app.modules.plugins.builtin.services import HostServices
from app.plugins.builtin.telegram.host import (
    HostUnavailableError,
    Recipient,
    TelegramHost,
    current_host,
)

PLUGIN_ID = "builtin.telegram"
MAX_CANDIDATES = 5

Target = Annotated[
    str,
    Field(
        min_length=1,
        max_length=120,
        description="Recipient: the ref (person:<id>, contact:<n>) or name (people.resolve).",
    ),
]
Text = Annotated[
    str,
    Field(min_length=1, max_length=4000, description="The message text only, as the user says it."),
]


def candidate_line(candidate: Recipient) -> str:
    relation = f", {candidate.relation}" if candidate.relation else ""
    return f"{candidate.name}{relation} ({candidate.ref})"


def build_server(
    services: HostServices, manifest: Mapping[str, Any], plugin_dir: Path
) -> MCPServer:
    kit = PluginKit(manifest, services)

    def host() -> TelegramHost:
        found = current_host()
        if found is None:
            raise ToolError("Telegram недоступен на этом сервере.")
        return found

    @kit.tool(
        "send_message",
        description=(
            "Prepare a Telegram message to a person: opens Telegram on the user's phone with the "
            "text ready; the user presses send. Resolve the person with people.resolve first."
        ),
        open_world=True,
    )
    async def send_message(to: Target, text: Text, *, ctx: Context) -> CallToolResult:
        identity = kit.identity(ctx)
        telegram = host()
        try:
            found = await telegram.candidates(identity, to.strip())
            if not found:
                raise ToolError(
                    f"Не нашла человека «{to}». Уточни имя у пользователя; ничего не отправлено."
                )
            if len(found) > 1:
                options = "; ".join(candidate_line(item) for item in found[:MAX_CANDIDATES])
                raise ToolError(
                    f"Подходит несколько человек: {options}. Спроси пользователя, "
                    "кому именно, и повтори с точным именем или ref. Ничего не отправлено."
                )
            recipient = found[0]
            outcome = await telegram.hand_off(identity, recipient, text)
        except HostUnavailableError as error:
            raise ToolError(str(error)) from None
        if outcome.status != "opened":
            raise ToolError(outcome.message)
        return ok(
            outcome.message,
            {
                "recipient": recipient.name,
                "opened": True,
                "sent": False,
                "direct_chat": outcome.direct_chat,
            },
        )

    @kit.tool(
        "connect_bot",
        description=(
            "Offer the user a one-time link that connects the Mia Telegram bot, which delivers "
            "notifications. The link is shown on screen, not in this result."
        ),
    )
    async def connect_bot(*, ctx: Context) -> CallToolResult:
        identity = kit.identity(ctx)
        try:
            offer = await host().bot_link(identity)
        except HostUnavailableError as error:
            raise ToolError(str(error)) from None
        if offer.already_linked:
            return ok(
                "Telegram-бот Mia уже подключён.",
                {"linked": True},
            )
        return ok(
            "Ссылка для подключения бота показана пользователю на экране; "
            "она одноразовая и действует недолго.",
            {"linked": False, "deep_link": offer.deep_link, "expires_at": offer.expires_at},
        )

    kit.verify_tools()
    return kit.server
