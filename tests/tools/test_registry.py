"""Manifests, manifest sources, skills and the tools digest."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.modules.plugins.registry import (
    MAX_PLUGIN_TOOLS,
    MODEL_TOOL_LIMIT,
    NEEDS_ATTENTION,
    PLUGIN_TOOL_BUDGET,
    DirectoryManifestSource,
    InstallationRecord,
    ManifestError,
    PluginRegistry,
    StaticInstallationReader,
    parse_manifest,
)
from app.modules.plugins.registry.limits import default_selection, keep_within, rank_tools
from app.modules.tools.digest import ServerTool, compute_tools_digest

from tests.tools.helpers import manifest, write_package

EXAMPLES = Path("contracts/mvp/v1/examples/plugin-manifest")
REGISTRY_LOGGER = "app.modules.plugins.registry.installed"


@pytest.fixture
def registry_log(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Registry warnings (Alembic's fileConfig in other tests may have disabled the logger)."""
    logger = logging.getLogger(REGISTRY_LOGGER)
    disabled, logger.disabled = logger.disabled, False
    with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
        yield caplog
    logger.disabled = disabled


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


async def test_registry_reports_unusable_installations_instead_of_dropping_them(
    registry_log: pytest.LogCaptureFixture,
) -> None:
    broken = {**manifest(), "schemaVersion": "mia.plugin/2"}
    records = [
        InstallationRecord("a", "w", "custom.abc", "0.0.0", "needs_consent", (), None, None),
        InstallationRecord("b", "w", "test.timer", "1.0.0", "active", (), None, broken),
        InstallationRecord(
            "c", "w", "test.timer", "9.9.9", "active", (), None, manifest()
        ),  # version mismatch with the manifest
        InstallationRecord("d", "w", "test.gone", "1.0.0", "active", (), None, None),
    ]
    registry = PluginRegistry(StaticInstallationReader({"u": records}))
    plugins = await registry.installed(user_id="u", workspace_id=None)
    # The custom server awaiting consent has nothing to offer yet; the others are reported.
    assert [(p.installation.id, p.installation.status, p.problem, p.reason) for p in plugins] == [
        ("b", NEEDS_ATTENTION, "manifest_invalid", "unsupported_schema"),
        ("c", NEEDS_ATTENTION, "manifest_mismatch", None),
        ("d", NEEDS_ATTENTION, "manifest_missing", None),
    ]
    # The router can still refuse the declared tools by name (``tool_not_active``).
    assert list(plugins[0].manifest.tools) == list(broken["tools"])
    assert [r.msg for r in registry_log.records] == [
        "plugin_manifest_invalid",
        "plugin_manifest_mismatch",
        "plugin_manifest_missing",
    ]
    assert registry_log.records[0].plugin_id == "test.timer"  # type: ignore[attr-defined]
    assert registry_log.records[0].reason == "unsupported_schema"  # type: ignore[attr-defined]


def _wide(plugin_id: str, names: list[str]) -> dict[str, Any]:
    body = manifest()
    return {
        **body,
        "id": plugin_id,
        "tools": {name: {"risk": "write_external"} for name in names},
    }


def test_one_limit_for_every_manifest() -> None:
    names = [f"list_things_{i:03d}" for i in range(MAX_PLUGIN_TOOLS)]
    assert len(parse_manifest(_wide("test.big", names)).tools) == MAX_PLUGIN_TOOLS == 96
    with pytest.raises(ManifestError) as error:
        parse_manifest(_wide("test.big", [*names, "one_more"]))
    assert error.value.code == "too_many_tools"
    assert MODEL_TOOL_LIMIT - PLUGIN_TOOL_BUDGET == 32
    schema = json.loads(Path("contracts/mvp/v1/plugin-manifest.schema.json").read_text("utf-8"))
    assert schema["properties"]["tools"]["maxProperties"] == MAX_PLUGIN_TOOLS


async def test_a_linear_sized_server_loads_and_plugins_share_the_budget(
    registry_log: pytest.LogCaptureFixture,
) -> None:
    small = _wide("test.small", [f"get_item_{i:02d}" for i in range(25)])
    linear = [f"list_issues_{i:02d}" for i in range(40)]
    linear += [f"create_issue_{i:02d}" for i in range(26)]
    linear += [f"delete_issue_{i:02d}" for i in range(10)]
    assert len(linear) == 76
    records = [
        InstallationRecord("s", "w", "test.small", "1.0.0", "active", (), "d", small),
        InstallationRecord(
            "l", "w", "custom.ulinear", "1.0.0", "active", (), "d", _wide("custom.ulinear", linear)
        ),
        InstallationRecord(
            "x", "w", "test.extra", "1.0.0", "active", (), "d", _wide("test.extra", ["get_x"])
        ),
    ]
    registry = PluginRegistry(StaticInstallationReader({"u": records}))
    small_plugin, big, extra = await registry.installed(user_id="u", workspace_id=None)
    assert small_plugin.problem is None and len(small_plugin.manifest.tools) == 25
    # 25 + 76 > 96: the later installation keeps 71 tools, destructive ones go first.
    assert big.active and big.problem == "tool_budget" and big.declared_tools == 76
    assert len(big.manifest.tools) == 71
    assert sum(name.startswith("delete_") for name in big.manifest.tools) == 5
    # Nothing is left for the next one: it is reported, not dropped.
    assert extra.installation.status == NEEDS_ATTENTION and extra.problem == "tool_budget"
    assert [r.msg for r in registry_log.records] == ["plugin_tools_over_budget"] * 2
    assert registry_log.records[0].tool_count == 76 and registry_log.records[0].limit == 71  # type: ignore[attr-defined]
    # What the custom server may enable next to the others (consent screen).
    assert await registry.tool_limit(user_id="u", workspace_id=None, installation_id="l") == 70
    assert await registry.tool_limit(user_id="u", workspace_id=None, installation_id="x") == 0


def test_tool_ranking_and_default_selection() -> None:
    names = ["delete_issue", "list_issues", "create_issue", "sync_all", "archive_project"]
    assert rank_tools(names) == [
        "list_issues",
        "create_issue",
        "sync_all",
        "delete_issue",
        "archive_project",
    ]
    assert keep_within(names, 2) == ["list_issues", "create_issue"]
    # A small server: everything is ticked; a large one starts without destructive tools.
    assert default_selection(names, 96) == names
    large = [f"get_{i}" for i in range(30)] + ["delete_all"]
    assert default_selection(large, 96) == large[:30]
    assert default_selection(large, 10) == large[:10]


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
