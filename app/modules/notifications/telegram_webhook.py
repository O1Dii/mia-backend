"""Register or remove the bot webhook.

Usage: ``python -m app.modules.notifications.telegram_webhook set|delete``.

Reads ``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_WEBHOOK_SECRET``, ``PUBLIC_BASE_URL`` from the
environment (``Settings``); the token and the secret are never printed.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import Settings, get_settings
from app.modules.notifications.telegram_api import HttpTelegramBotApi

WEBHOOK_PATH = "/webhooks/telegram"


def webhook_url(settings: Settings) -> str:
    return settings.public_base_url.rstrip("/") + WEBHOOK_PATH


async def run(action: str, settings: Settings) -> int:
    token = settings.telegram_bot_token
    if token is None:
        print("TELEGRAM_BOT_TOKEN is not set")
        return 2
    api = HttpTelegramBotApi(
        token.get_secret_value(),
        base_url=settings.telegram_api_base,
        timeout_s=settings.telegram_request_timeout_seconds,
    )
    if action == "delete":
        ok = await api.delete_webhook()
    else:
        secret = settings.telegram_webhook_secret
        if secret is None:
            print("TELEGRAM_WEBHOOK_SECRET is not set")
            return 2
        if not settings.public_base_url.startswith("https://"):
            print("PUBLIC_BASE_URL must be https for a Telegram webhook")
            return 2
        ok = await api.set_webhook(webhook_url(settings), secret.get_secret_value())
    print("ok" if ok else "failed")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in {"set", "delete"}:
        print("usage: python -m app.modules.notifications.telegram_webhook set|delete")
        return 2
    return asyncio.run(run(args[0], get_settings()))


if __name__ == "__main__":
    raise SystemExit(main())
