"""Manifests, manifest sources, skills and the tools digest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.modules.plugins.registry import (
    DirectoryManifestSource,
    InstallationRecord,
    ManifestError,
    PluginRegistry,
    StaticInstallationReader,
    parse_manifest,
)
from app.modules.tools.digest import ServerTool, compute_tools_digest

from tests.tools.helpers import manifest, write_package

EXAMPLES = Path("contracts/mvp/v1/examples/plugin-manifest")


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.glob("*.mia.json")))
def test_contract_examples_parse(name: str) -> None:
    parsed = parse_manifest(json.loads((EXAMPLES / name).read_text("utf-8")))
    assert parsed.tools and parsed.plugin_id


def test_router_relevant_fields() -> None:
    linear = parse_manifest(json.loads((EXAMPLES / "linear.mia.json").read_text("utf-8")))
    assert linear.server.kind == "remote" and linear.server.auth == "oauth"
    assert linear.server.host == "mcp.linear.app" and linear.network == {"mcp.linear.app"}
    assert linear.tools["delete_issue"].risk == "write_external"
    timer = parse_manifest(json.loads((EXAMPLES / "timer.mia.json").read_text("utf-8")))
    assert timer.server.in_process and timer.tools["start"].voice
    assert timer.device_tools == ("device.alarm.set",)


@pytest.mark.parametrize(
    "change",
    [
        {"schemaVersion": "mia.plugin/2"},
        {"id": "Bad Id"},
        {"server": {"kind": "builtin", "module": "os.system"}},
        {"server": {"kind": "remote", "url": "http://mcp.test.example/mcp", "auth": "none"}},
        {"server": {"kind": "stdio"}},
        {"tools": {}},
        {"tools": {"Bad-Name": {"risk": "read"}}},
        {"tools": {"x": {"risk": "dangerous"}}},
        {"tools": {"x": {"risk": "read", "timeoutSeconds": 0}}},
        {"skills": ["../etc/passwd"]},
    ],
)
def test_invalid_manifests_are_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ManifestError):
        parse_manifest({**manifest(), **change})


async def test_directory_source_and_skills(tmp_path: Path) -> None:
    body = manifest()
    write_package(tmp_path, body)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "mia.json").write_text("{not json", "utf-8")
    source = DirectoryManifestSource(tmp_path, tmp_path / "missing")
    loaded = await source.load("test.timer", "1.0.0")
    assert loaded is not None and loaded.plugin_dir == tmp_path / "test.timer"
    assert await source.load("test.timer", "2.0.0") is None

    record = InstallationRecord(
        id="i1",
        workspace_id="w1",
        plugin_id="test.timer",
        version="1.0.0",
        status="active",
        grants=(),
        tools_digest=None,
        manifest=None,
    )
    registry = PluginRegistry(StaticInstallationReader({"u1": [record]}), source)
    [plugin] = await registry.installed(user_id="u1", workspace_id=None)
    summaries = await registry.skill_summaries(plugin)
    assert [s.description for s in summaries] == [
        "Use for countdown timers; start them with seconds."
    ]
    assert "Always confirm" in (await registry.skill_bodies(plugin))[0]


async def test_registry_skips_unusable_installations() -> None:
    records = [
        InstallationRecord("a", "w", "custom.abc", "0.0.0", "needs_consent", (), None, None),
        InstallationRecord("b", "w", "test.timer", "1.0.0", "active", (), None, {"id": "x"}),
        InstallationRecord(
            "c", "w", "test.timer", "9.9.9", "active", (), None, manifest()
        ),  # version mismatch with the manifest
    ]
    registry = PluginRegistry(StaticInstallationReader({"u": records}))
    assert await registry.installed(user_id="u", workspace_id=None) == []


def test_digest_is_canonical_and_sensitive() -> None:
    a = ServerTool("start", "Start.", {"type": "object", "properties": {"s": {"type": "integer"}}})
    b = ServerTool("get", "Get.", {"type": "object"})
    digest = compute_tools_digest([a, b])
    assert digest == compute_tools_digest([b, a])
    assert digest == compute_tools_digest(
        [
            {"name": "get", "description": "Get.", "inputSchema": {"type": "object"}},
            {"name": "start", "description": "Start.", "input_schema": dict(a.input_schema)},
        ]
    )
    assert digest.startswith("sha256:") and len(digest) == 71
    changed = ServerTool("start", "Start!", a.input_schema)
    assert compute_tools_digest([changed, b]) != digest
