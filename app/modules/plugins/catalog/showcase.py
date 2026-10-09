"""Showcase data of the builtin plugins, carried over from ``web/src/plugins/catalog.ts``.

``mia.json`` has no place for presentation hints, so tagline, colour and art live in
``plugin_releases.store_meta``. ``legacy_id`` is the web POC id: reviews imported from the
POC (``plugin_reviews.plugin_id``) are shown on the plugin that replaced it.
``mia.clock`` (world clock) was not ported to MCP (OI-M14-003), so it has no entry.
"""

from __future__ import annotations

from typing import Any, Final

BUILTIN_SHOWCASE: Final[dict[str, dict[str, Any]]] = {
    "builtin.news": {
        "legacy_id": "mia.digest",
        "tagline": "Меньше шума. Больше смысла.",
        "color": "violet",
        "art": "digest",
        "ai": True,
    },
    "builtin.timer": {
        "legacy_id": "mia.timer",
        "tagline": "У всего есть своё время.",
        "color": "mint",
        "art": "timer",
    },
    "builtin.notes": {
        "legacy_id": "mia.notes",
        "tagline": "Хорошие идеи не ждут.",
        "color": "peach",
        "art": "notes",
    },
    "builtin.focus": {
        "legacy_id": "mia.focus",
        "tagline": "Одна задача. Весь фокус.",
        "color": "yellow",
        "art": "focus",
    },
    "builtin.alarm": {
        "legacy_id": "mia.alarm",
        "tagline": "Начните в своём ритме.",
        "color": "pink",
        "art": "alarm",
    },
    "builtin.internet": {
        "legacy_id": "mia.web",
        "tagline": "Новые знания для любой модели.",
        "color": "blue",
        "art": "clock",
        "ai": True,
    },
}

CATEGORY_TITLES: Final[dict[str, str]] = {
    "work": "Работа",
    "productivity": "Продуктивность",
    "communication": "Общение",
    "knowledge": "Знания",
    "home": "Дом",
    "utilities": "Повседневное",
    "fun": "Развлечения",
}
"""Russian titles of the manifest ``listing.category`` values (web POC: Повседневное,
Продуктивность, AI map to utilities, productivity, knowledge)."""


def showcase_for(plugin_id: str) -> dict[str, Any]:
    return dict(BUILTIN_SHOWCASE.get(plugin_id, {}))
