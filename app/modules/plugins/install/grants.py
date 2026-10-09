"""The grants vocabulary: what a user consents to when installing or updating a plugin.

``installations.grants`` stores exactly the set :func:`required_grants` derives from the
consented ``mia.json``. A grant is a short string:

==========================  ==============================================================
``tools.read``              the plugin has tools of risk ``read``
``tools.write_reversible``  ... of risk ``write_reversible`` (undoable changes, no approval)
``tools.write_external``    ... of risk ``write_external`` (every call asks for approval)
``network:<host>``          the plugin may reach this host (``permissions.network``)
``network.public_web``      the plugin may read any public HTTPS page (``permissions.publicWeb``)
``llm.generate``            the plugin may use the user's model through Mia (``llm.viaHost``)
``memory.context``          Mia injects relevant memory for the plugin (``memory: context``)
``memory.suggest``          the plugin may suggest facts (land as ``suggested``)
``memory.read:<scope>``     raw memory reads of one scope (``profile``, ``work``, ...)
``notify``                  the plugin may notify the user
``schedules``               the plugin may schedule background work
==========================  ==============================================================

A new version that needs a grant the installation does not hold is an *expansion*: the
update is refused with ``consent_required`` until the user consents to the added grants.
Tool descriptions are covered separately by ``tools_digest`` (the router blocks a changed
digest until consent). The router may check ``required_grants(manifest) <= installation.grants``
in ``McpToolRouter._plugin_entries`` (OI-M13-002).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

RISK_GRANTS = {
    "read": "tools.read",
    "write_reversible": "tools.write_reversible",
    "write_external": "tools.write_external",
}

_TEXTS = {
    "tools.read": "Читать данные плагина и сервиса, не меняя их",
    "tools.write_reversible": "Менять данные плагина (изменения можно отменить)",
    "tools.write_external": "Действия во внешнем сервисе (каждое — после вашего подтверждения)",
    "network.public_web": "Искать в интернете и читать публичные HTTPS-страницы",
    "llm.generate": "Отправлять текст вашей модели через Mia",
    "memory.context": "Получать уместную память о вас в запросах",
    "memory.suggest": "Предлагать факты для памяти (вы подтверждаете сами)",
    "notify": "Присылать уведомления",
    "schedules": "Запускать работу по расписанию",
}


def required_grants(manifest: Mapping[str, Any]) -> frozenset[str]:
    """Grants a ``mia.json`` asks for (the consent set)."""
    grants: set[str] = set()
    tools = manifest.get("tools")
    if isinstance(tools, Mapping):
        for policy in tools.values():
            risk = policy.get("risk") if isinstance(policy, Mapping) else None
            if isinstance(risk, str) and risk in RISK_GRANTS:
                grants.add(RISK_GRANTS[risk])
    permissions = manifest.get("permissions")
    if isinstance(permissions, Mapping):
        network = permissions.get("network")
        if isinstance(network, list):
            grants.update(f"network:{host}" for host in network if isinstance(host, str))
        if permissions.get("publicWeb") is True:
            grants.add("network.public_web")
        llm = permissions.get("llm")
        if isinstance(llm, Mapping) and llm.get("viaHost") is True:
            grants.add("llm.generate")
        memory = permissions.get("memory")
        if isinstance(memory, list):
            grants.update(f"memory.{entry}" for entry in memory if isinstance(entry, str))
        if permissions.get("notify") is True:
            grants.add("notify")
        if permissions.get("schedules") is True:
            grants.add("schedules")
    return frozenset(grants)


@dataclass(frozen=True, slots=True)
class GrantDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def expanded(self) -> bool:
        return bool(self.added)


def diff_grants(installed: Iterable[str], required: Iterable[str]) -> GrantDiff:
    have, need = set(installed), set(required)
    return GrantDiff(added=tuple(sorted(need - have)), removed=tuple(sorted(have - need)))


def describe_grant(grant: str) -> str:
    """Human-readable consent line (Russian, the product language)."""
    if grant in _TEXTS:
        return _TEXTS[grant]
    if grant.startswith("network:"):
        return f"Обращаться к сайту {grant.removeprefix('network:')}"
    if grant.startswith("memory.read:"):
        return f"Читать записи памяти: {grant.removeprefix('memory.read:')}"
    return grant
