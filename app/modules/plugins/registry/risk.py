"""Risk of the tools of a server added by URL: chosen by the user, or derived by Mia (M72-R1).

``mia.json`` ``tools.<name>.riskSource`` tells where a stored risk came from:

* ``user`` - the user picked it on the add-by-URL screen; never re-derived;
* ``annotation`` / ``default`` - Mia derived it from the server's MCP annotations at consent
  (``default``: the server said nothing, so ``write_external``);
* absent - an installation consented before M72-R1. Those risks were all ``write_external``
  (before M72) or derived (M72), and there is no record of a user's edit, so they count as
  derived and are re-derived once, the next time the router lists the server's tools.

Annotations are not part of the tools digest, so a listing whose digest still matches carries
them without blocking the plugin. The router calls :func:`rederive_risks` after the digest check,
uses the result for the call it is serving and hands it to a :class:`RiskStore`, which freezes it
in the installation row (with markers, so the next listing finds nothing to do). Once marked, a
derived risk follows the annotations only upward: a server that later calls a tool destructive
makes it ask more, one that later calls it harmless changes nothing until the user consents again.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Mapping
from typing import Any, Protocol, cast

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.types import Risk
from app.modules.workspace.models import Installation

logger = logging.getLogger(__name__)

USER = "user"
_ORDER: dict[str, int] = {"read": 0, "write_reversible": 1, "write_external": 2}


def risk_from_annotations(hints: Mapping[str, Any] | None) -> tuple[Risk, str]:
    """Risk and ``riskSource`` of a tool from its MCP annotations (OI-LIVE-002, OI-M72-001).

    ``readOnlyHint`` -> ``read`` (no approval); ``destructiveHint: false`` ->
    ``write_reversible``; anything else, including no annotations, ``write_external`` (MCP's
    own default is "destructive", and a server that says nothing is not trusted).
    """
    hints = hints or {}
    if hints.get("readOnlyHint") is True:
        return "read", "annotation"
    if hints.get("destructiveHint") is False:
        return "write_reversible", "annotation"
    return "write_external", "annotation" if hints.get("destructiveHint") is True else "default"


def rederive_risks(
    manifest: Mapping[str, Any], served: Mapping[str, Mapping[str, Any] | None]
) -> dict[str, Any] | None:
    """The stored manifest with derived risks recomputed from ``served`` annotations.

    ``served`` maps each tool the server lists now to its annotations. A tool with
    ``riskSource: user`` and a tool the server does not list are left alone. A tool without a
    marker (stored before M72-R1) takes the derived risk, up or down; a marked one only moves up.
    Returns ``None`` when nothing changes.
    """
    tools = manifest.get("tools")
    if not isinstance(tools, Mapping):
        return None
    changes: dict[str, dict[str, Any]] = {}
    for name, policy in tools.items():
        if not isinstance(policy, Mapping) or policy.get("riskSource") == USER:
            continue
        if name not in served:
            continue
        risk, source = risk_from_annotations(served[name])
        stored = str(policy.get("risk"))
        if policy.get("riskSource") is None:
            moves = True
        else:
            moves = _ORDER[risk] > _ORDER.get(stored, 2)
        if moves:
            changes[name] = {**policy, "risk": risk, "riskSource": source}
    if not changes:
        return None
    upgraded = copy.deepcopy(dict(manifest))
    upgraded["tools"] = {**tools, **changes}
    return upgraded


class RiskStore(Protocol):
    async def save(
        self, installation_id: str, before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> None:
        """Replace ``before`` by ``after`` as the stored manifest, if ``before`` is still stored."""
        ...


class SqlRiskStore:
    """Freezes re-derived risks in the installation row.

    The row is touched only while its manifest is still exactly ``before`` (a consent that
    happened in between wins) and only for an MCP server added by URL. ``state_version`` is
    not bumped: nothing the user consented to changes, so an open consent screen stays valid.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(
        self, installation_id: str, before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> None:
        try:
            key = uuid.UUID(installation_id)
        except ValueError:
            return
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(Installation)
                .where(
                    Installation.id == key,
                    Installation.source == "custom",
                    Installation.manifest == cast(dict[str, Any], dict(before)),
                )
                .values(
                    manifest=cast(dict[str, Any], dict(after)), updated_at=Installation.updated_at
                )
            )
