"""Typed view of the ``mia.json`` fields the tool router relies on.

Schema: ``contracts/mvp/v1/plugin-manifest.schema.json``. Publication (M15) and
builtin packaging (M14) validate the full document; the router re-checks only
what it enforces at call time, defensively, because a manifest stored in an
installation row is data and must never be trusted blindly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlsplit

from app.core.ports.types import JsonObject, Risk

PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}(\.[a-z][a-z0-9-]{1,47}){1,2}$")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MODULE = re.compile(r"^app(\.[a-z_][a-z0-9_]*)+$")
RELATIVE_PATH = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$")
RISKS: frozenset[str] = frozenset({"read", "write_reversible", "write_external"})

ServerKind = Literal["builtin", "remote", "declarative"]
RemoteAuth = Literal["none", "oauth", "mia_token"]


class ManifestError(ValueError):
    """The manifest is missing or violates a rule the router enforces."""


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Mia policy overlay for one server tool (``mia.json`` ``tools.<name>``)."""

    risk: Risk
    voice: bool = False
    timeout_seconds: int | None = None
    task: bool = False


@dataclass(frozen=True, slots=True)
class ServerSpec:
    kind: ServerKind
    module: str | None = None
    """``builtin``: importable module of the in-process MCP server."""
    url: str | None = None
    """``remote``: Streamable HTTP endpoint (https)."""
    auth: RemoteAuth = "none"
    protocol_version: str | None = None

    @property
    def in_process(self) -> bool:
        return self.kind in ("builtin", "declarative")

    @property
    def host(self) -> str | None:
        return urlsplit(self.url).hostname if self.url else None


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    version: str
    name: str
    server: ServerSpec
    tools: Mapping[str, ToolPolicy]
    skills: tuple[str, ...]
    network: frozenset[str]
    """``permissions.network``: hosts the plugin may reach (SSRF allow-list)."""
    device_tools: tuple[str, ...]
    raw: JsonObject
    """The full manifest as stored; handed to the in-process server factory."""


def _object(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{where} must be an object")
    return cast(Mapping[str, object], value)


def _string(value: object, where: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where} must be a non-empty string")
    if pattern is not None and not pattern.match(value):
        raise ManifestError(f"{where} has an invalid format")
    return value


def _server(raw: Mapping[str, object], network: frozenset[str]) -> ServerSpec:
    kind = raw.get("kind")
    if kind == "builtin":
        return ServerSpec(
            kind="builtin", module=_string(raw.get("module"), "server.module", pattern=MODULE)
        )
    if kind == "declarative":
        return ServerSpec(kind="declarative")
    if kind == "remote":
        url = _string(raw.get("url"), "server.url")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ManifestError("server.url must be a plain https URL")
        if parts.hostname.lower() not in network:
            raise ManifestError("server.url host must be listed in permissions.network")
        auth = raw.get("auth", "none")
        if auth not in ("none", "oauth", "mia_token"):
            raise ManifestError("server.auth is invalid")
        version = raw.get("protocolVersion")
        return ServerSpec(
            kind="remote",
            url=url,
            auth=cast(RemoteAuth, auth),
            protocol_version=version if isinstance(version, str) else None,
        )
    raise ManifestError("server.kind is invalid")


def _tool_policy(name: str, raw: object) -> ToolPolicy:
    body = _object(raw, f"tools.{name}")
    risk = body.get("risk")
    if risk not in RISKS:
        raise ManifestError(f"tools.{name}.risk is invalid")
    timeout = body.get("timeoutSeconds")
    if timeout is not None and (
        not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300
    ):
        raise ManifestError(f"tools.{name}.timeoutSeconds is invalid")
    return ToolPolicy(
        risk=cast(Risk, risk),
        voice=body.get("voice") is True,
        timeout_seconds=timeout,
        task=body.get("task") is True,
    )


def parse_manifest(raw: object) -> PluginManifest:
    """Parse the router-relevant part of ``mia.json``; raise ``ManifestError``."""
    body = _object(raw, "manifest")
    if body.get("schemaVersion") != "mia.plugin/1":
        raise ManifestError("schemaVersion must be mia.plugin/1")
    plugin_id = _string(body.get("id"), "id", pattern=PLUGIN_ID)
    version = _string(body.get("version"), "version")
    name = _string(body.get("name"), "name")
    permissions = _object(body.get("permissions"), "permissions")
    hosts = permissions.get("network", [])
    if not isinstance(hosts, list) or not all(
        isinstance(host, str) and HOSTNAME.match(host) for host in hosts
    ):
        raise ManifestError("permissions.network must list hostnames")
    network = frozenset(cast(list[str], hosts))
    server = _server(_object(body.get("server"), "server"), network)
    tools_raw = _object(body.get("tools"), "tools")
    if not tools_raw or len(tools_raw) > 64:
        raise ManifestError("tools must declare 1..64 tools")
    tools: dict[str, ToolPolicy] = {}
    for tool_name, policy in tools_raw.items():
        if not TOOL_NAME.match(tool_name):
            raise ManifestError("tool names must match the toolName pattern")
        tools[tool_name] = _tool_policy(tool_name, policy)
    skills_raw = body.get("skills", [])
    if not isinstance(skills_raw, list) or not all(
        isinstance(path, str) and RELATIVE_PATH.match(path) for path in skills_raw
    ):
        raise ManifestError("skills must be relative paths")
    requires = body.get("requires")
    device_tools: tuple[str, ...] = ()
    if isinstance(requires, Mapping):
        declared = requires.get("deviceTools", [])
        if isinstance(declared, list):
            device_tools = tuple(item for item in declared if isinstance(item, str))
    return PluginManifest(
        plugin_id=plugin_id,
        version=version,
        name=name,
        server=server,
        tools=tools,
        skills=tuple(cast(list[str], skills_raw)),
        network=network,
        device_tools=device_tools,
        raw=body,
    )
