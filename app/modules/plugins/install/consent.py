"""The consent screen of a release: tools with risk, data, network, model use, grants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.modules.plugins.catalog.schemas import (
    ConsentDeviceCapability,
    ConsentGrant,
    ConsentLlm,
    ConsentTool,
    ConsentView,
    WhereItRuns,
)
from app.modules.plugins.install.grants import describe_grant, required_grants
from app.modules.tools.device_policy import CAPABILITIES, capability_for


def requires_connection(manifest: Mapping[str, Any]) -> bool:
    server = manifest.get("server")
    return (
        isinstance(server, Mapping)
        and server.get("kind") == "remote"
        and server.get("auth") in {"oauth", "mia_token"}
    )


def device_capabilities(manifest: Mapping[str, Any]) -> list[ConsentDeviceCapability]:
    """``requires.deviceCapabilities`` for the consent screen with the policy's labels."""
    requires = manifest.get("requires") or {}
    declared = requires.get("deviceCapabilities") if isinstance(requires, Mapping) else None
    result: list[ConsentDeviceCapability] = []
    seen: set[str] = set()
    for item in declared if isinstance(declared, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("capability"), str):
            continue
        name = str(item["capability"])
        if name in seen:
            continue
        seen.add(name)
        capability = CAPABILITIES.get(name) or capability_for(name)
        result.append(
            ConsentDeviceCapability(
                capability=name,
                kind=capability.kind,
                label=capability.label,
                description=str(item.get("description", "")),
                optional=item.get("optional") is not False,
                model_sees=capability.model_sees,
            )
        )
    return result


def where_it_runs(
    manifest: Mapping[str, Any], capabilities: list[ConsentDeviceCapability]
) -> WhereItRuns:
    """The «Где работает» block: declared lines, else derived from the server kind."""
    declared = manifest.get("whereItRuns")
    declared = declared if isinstance(declared, Mapping) else {}
    server = manifest.get("server")
    kind = server.get("kind") if isinstance(server, Mapping) else None
    cloud = declared.get("cloud")
    if not isinstance(cloud, str) or not cloud:
        cloud = (
            "Работает на сервере Mia."
            if kind != "remote"
            else "Работает на стороннем сервере; Mia обращается к нему из облака."
        )
    device = declared.get("device")
    if not isinstance(device, str) or not device:
        device = None
    if device is None and capabilities:
        device = "; ".join(item.description for item in capabilities)
    return WhereItRuns(
        cloud=cloud,
        device=device,
        runs_on="cloud_and_device" if device is not None else "cloud",
    )


def build_consent(
    manifest: Mapping[str, Any], tools: list[dict[str, Any]], tools_digest: str
) -> ConsentView:
    """``tools`` is the release snapshot: ``{name, description, risk, voice}`` per tool."""
    permissions = manifest.get("permissions") or {}
    llm = permissions.get("llm") or {}
    requires = manifest.get("requires") or {}
    budget = llm.get("monthlyBudgetUsd")
    capabilities = device_capabilities(manifest)
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
        where_it_runs=where_it_runs(manifest, capabilities),
        device_capabilities=capabilities,
    )
