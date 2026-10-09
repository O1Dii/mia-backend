"""Build ``assistant-response.schema.json`` objects for inbox items from plain text.

``speech`` must be one or two plain sentences: no line breaks, markdown, links or
angle brackets. User-provided text (timer labels, notify text) is sanitized here.
"""

from __future__ import annotations

import re
from typing import Any

SPEECH_LIMIT = 250
TITLE_LIMIT = 200
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_FORBIDDEN = re.compile(r"[\r\n*`#|\[\]<>]+")
_SPACES = re.compile(r"\s+")


def plain(text: str, limit: int = SPEECH_LIMIT, *, fallback: str = "Готово.") -> str:
    """One line of plain text without markdown/links, clipped at a word boundary."""
    cleaned = _SPACES.sub(" ", _FORBIDDEN.sub(" ", _URL.sub("", text))).strip()
    if not cleaned:
        return fallback
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"


def title(text: str) -> str:
    return plain(text, TITLE_LIMIT, fallback="Mia")


def text_response(speech: str, markdown: str | None = None) -> dict[str, Any]:
    """A response whose screen part is ``markdown`` (or nothing when it equals speech)."""
    spoken = plain(speech)
    display: dict[str, Any] | None = None
    if markdown is not None and markdown.strip() and markdown.strip() != spoken:
        display = {"kind": "text", "markdown": markdown.strip()[:32000]}
    return {"speech": spoken, "display": display, "followups": []}
