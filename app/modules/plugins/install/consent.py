"""The consent screen of a release: tools with risk, data, network, model use, grants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.modules.plugins.catalog.schemas import (
    ConsentGrant,
    ConsentLlm,
    ConsentTool,
    ConsentView,
)
from app.modules.plugins.install.grants import describe_grant, required_grants


def requires_connection(manifest: Mapping[str, Any]) -> bool:
    server = manifest.get("server")
    return (
        isinstance(server, Mapping)
        and server.get("kind") == "remote"
        and server.get("auth") in {"oauth", "mia_token"}
    )


def build_consent(
    manifest: Mapping[str, Any], tools: list[dict[str, Any]], tools_digest: str
) -> ConsentView:
    """``tools`` is the release snapshot: ``{name, description, risk, voice}`` per tool."""
    permissions = manifest.get("permissions") or {}
    llm = permissions.get("llm") or {}
    requires = manifest.get("requires") or {}
    budget = llm.get("monthlyBudgetUsd")
    return ConsentView(
        plugin_id=str(manifest["id"]),
        version=str(manifest["version"]),
        tools_digest=tools_digest,
        tools=[
            ConsentTool(
                name=str(tool["name"]),
                description=str(tool.get("description", "")),
                risk=tool["risk"],
                voice=bool(tool.get("voice", False)),
            )
            for tool in tools
        ],
        grants=[
            ConsentGrant(id=grant, text=describe_grant(grant))
            for grant in sorted(required_grants(manifest))
        ],
        network=sorted(permissions.get("network", [])),
        public_web=permissions.get("publicWeb") is True,
        llm=ConsentLlm(
            via_host=llm.get("viaHost") is True,
            monthly_budget_usd=float(budget) if isinstance(budget, int | float) else None,
        ),
        memory=sorted(permissions.get("memory", [])),
        notify=permissions.get("notify") is True,
        schedules=permissions.get("schedules") is True,
        device_tools=sorted(requires.get("deviceTools", [])),
        requires_connection=requires_connection(manifest),
    )
