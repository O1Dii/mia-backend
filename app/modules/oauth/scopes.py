"""Scope vocabulary of «Войти через Mia» (ADR-0013, Review §8.1).

A scope string is parsed once at ``/oauth/authorize``; anything outside the
vocabulary is ``invalid_scope``. ``offline_access`` is accepted and dropped:
Mia always issues a rotating refresh token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, cast

from app.core.ports.memory_provider import MemoryCategory
from app.modules.memory.domain import CATEGORIES, SENSITIVE_CATEGORIES

IDENTITY_SCOPES: Final = ("openid", "profile", "email")
FIXED_SCOPES: Final = frozenset(
    {
        *IDENTITY_SCOPES,
        "llm.generate",
        "memory.context",
        "memory.suggest",
        "tools.use",
        "jobs",
        "notify",
        "mia.install",
    }
)
IGNORED_SCOPES: Final = frozenset({"offline_access"})
MEMORY_READ_PREFIX: Final = "memory.read:"
TOOLS_USE_PREFIX: Final = "tools.use:"
# Same grammar as ``app.modules.plugins.registry.manifest.PLUGIN_ID``.
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}(\.[a-z][a-z0-9-]{1,47}){1,2}$")
MAX_SCOPES: Final = 32

DEFAULT_CONTEXT_CATEGORIES: Final[tuple[MemoryCategory, ...]] = tuple(
    c for c in CATEGORIES if c not in SENSITIVE_CATEGORIES
)
"""Categories the core may inject for ``memory.context`` unless the user narrows them."""

NOT_ENFORCED_YET: Final = frozenset({"jobs", "notify", "mia.install"})
"""Consentable, but no app-facing endpoint exists yet (OI-M50-002)."""


class InvalidScopeError(ValueError):
    """The scope string names something outside the vocabulary."""


def is_valid_scope(scope: str) -> bool:
    if scope in FIXED_SCOPES:
        return True
    if scope.startswith(MEMORY_READ_PREFIX):
        return scope[len(MEMORY_READ_PREFIX) :] in CATEGORIES
    if scope.startswith(TOOLS_USE_PREFIX):
        return bool(_PLUGIN_ID.match(scope[len(TOOLS_USE_PREFIX) :]))
    return False


def parse_scope(value: str | None) -> tuple[str, ...]:
    """Split, validate and de-duplicate a space-separated scope string (order kept)."""
    if not value or not value.strip():
        raise InvalidScopeError("scope is required")
    seen: dict[str, None] = {}
    for item in value.split(" "):
        if not item or item in IGNORED_SCOPES:
            continue
        if not is_valid_scope(item):
            raise InvalidScopeError("unknown scope")
        seen[item] = None
    if not seen or len(seen) > MAX_SCOPES:
        raise InvalidScopeError("scope is empty or too long")
    return tuple(seen)


def readable_categories(scopes: frozenset[str]) -> frozenset[MemoryCategory]:
    """Categories the app may read raw (``memory.read:<category>``)."""
    return frozenset(
        cast(MemoryCategory, s[len(MEMORY_READ_PREFIX) :])
        for s in scopes
        if s.startswith(MEMORY_READ_PREFIX)
    )


def plugins_allowed(scopes: frozenset[str]) -> frozenset[str] | None:
    """``None`` = every installed plugin (bare ``tools.use``); otherwise the plugin ids."""
    if "tools.use" in scopes:
        return None
    return frozenset(s[len(TOOLS_USE_PREFIX) :] for s in scopes if s.startswith(TOOLS_USE_PREFIX))


@dataclass(frozen=True, slots=True)
class ScopeText:
    scope: str
    title: str
    description: str
    sensitive: bool = False


_CATEGORY_RU: Final[dict[str, str]] = {
    "profile": "о вас",
    "work": "о работе",
    "home": "о доме",
    "health": "о здоровье",
    "finance": "о финансах",
    "interests": "об интересах",
    "other": "прочее и люди",
}

_FIXED_TEXT: Final[dict[str, tuple[str, str, bool]]] = {
    "openid": ("Узнать, что это вы", "Постоянный идентификатор вашего аккаунта Mia.", False),
    "profile": ("Имя и фото", "Имя и аватар из вашего аккаунта.", False),
    "email": ("Адрес почты", "Электронная почта вашего аккаунта.", False),
    "llm.generate": (
        "Пользоваться вашей моделью",
        "Запросы к вашей активной модели через Mia в пределах месячного лимита. "
        "Ключ провайдера приложению не передаётся.",
        False,
    ),
    "memory.context": (
        "Подмешивать контекст памяти",
        "Mia сама добавит к запросам приложения подходящие факты выбранных категорий. "
        "Сами факты приложению не отдаются.",
        False,
    ),
    "memory.suggest": (
        "Предлагать факты для памяти",
        "Предложения попадут в «Предложенные»; без вашего подтверждения ничего не сохранится.",
        False,
    ),
    "tools.use": (
        "Все установленные плагины",
        "Действия через любые ваши плагины; внешние действия — только после подтверждения.",
        True,
    ),
    "jobs": ("Фоновые задачи", "Пока не используется: доступ появится в следующих версиях.", False),
    "notify": ("Уведомления", "Пока не используется: доступ появится в следующих версиях.", False),
    "mia.install": (
        "Установка как плагин",
        "Пока не используется: доступ появится в следующих версиях.",
        False,
    ),
}


def describe(scope: str) -> ScopeText:
    """Russian consent-screen text for one valid scope."""
    if scope in _FIXED_TEXT:
        title, description, sensitive = _FIXED_TEXT[scope]
        return ScopeText(scope, title, description, sensitive)
    if scope.startswith(MEMORY_READ_PREFIX):
        category = scope[len(MEMORY_READ_PREFIX) :]
        return ScopeText(
            scope,
            f"Читать память: {_CATEGORY_RU.get(category, category)}",
            "Приложение получит сами факты этой категории; каждое чтение попадает в журнал.",
            sensitive=True,
        )
    plugin_id = scope[len(TOOLS_USE_PREFIX) :]
    return ScopeText(
        scope,
        f"Плагин {plugin_id}",
        "Действия через этот плагин; внешние действия — только после подтверждения.",
    )
