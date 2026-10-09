"""Every builtin ``mia.json`` is valid against the M00 contract and consistent with its package."""

from __future__ import annotations

import re

import pytest
from app.modules.plugins.builtin import BUILTIN_ROOT, BuiltinManifestSource

from tests.fakes.contracts import assert_mvp_valid, validator_module

PLUGIN_IDS = [
    "builtin.alarm",
    "builtin.focus",
    "builtin.internet",
    "builtin.news",
    "builtin.notes",
    "builtin.timer",
]
"""The six no-approval plugins of M14; ``builtin.telegram`` (M41, ``write_external``) has
its own tests in ``tests/plugins/telegram``."""
ALL_IDS = sorted([*PLUGIN_IDS, "builtin.telegram"])
SOURCE = BuiltinManifestSource()


def test_source_lists_exactly_the_known_plugins() -> None:
    assert [plugin.plugin_id for plugin in SOURCE.list()] == ALL_IDS
    assert {plugin.version for plugin in SOURCE.list()} == {"1.0.0"}
    assert SOURCE.get("builtin.timer", "1.0.0") is not None
    assert SOURCE.get("builtin.timer", "9.9.9") is None
    assert SOURCE.get("builtin.nope") is None
    assert SOURCE.get("builtin.timer") is SOURCE.get("builtin.timer", "1.0.0")
    assert BUILTIN_ROOT.name == "builtin"


@pytest.mark.parametrize("plugin_id", PLUGIN_IDS)
def test_manifest_matches_contract_and_semantics(plugin_id: str) -> None:
    plugin = SOURCE.get(plugin_id)
    assert plugin is not None
    manifest = dict(plugin.manifest)
    assert_mvp_valid(manifest, "plugin-manifest")
    validator_module().check_manifest_semantics(manifest, plugin_id)
    assert manifest["server"] == {
        "kind": "builtin",
        "module": f"app.plugins.builtin.{plugin_id.split('.')[1]}.server",
    }
    assert re.fullmatch(r"^app(\.[a-z_][a-z0-9_]*)+$", manifest["server"]["module"])
    assert plugin.directory.name == plugin_id.split(".")[1]


@pytest.mark.parametrize("plugin_id", PLUGIN_IDS)
def test_every_tool_has_risk_and_voice_is_safe(plugin_id: str) -> None:
    plugin = SOURCE.get(plugin_id)
    assert plugin is not None
    for name, policy in plugin.manifest["tools"].items():
        assert policy["risk"] in {"read", "write_reversible"}, name
        assert "write_external" != policy["risk"], "builtin tools never need approval"
        if policy.get("voice"):
            # Voice sees only fast reads or explicitly voice-safe reversible commands.
            assert policy["risk"] in {"read", "write_reversible"}
    assert any(policy["risk"] == "read" for policy in plugin.manifest["tools"].values())


@pytest.mark.parametrize("plugin_id", PLUGIN_IDS)
def test_package_files_exist(plugin_id: str) -> None:
    plugin = SOURCE.get(plugin_id)
    assert plugin is not None
    skill = plugin.skill_text()
    assert skill.startswith("---\nname: ")
    assert "description:" in skill.splitlines()[2]
    assert len(skill.splitlines()) <= 20, "SKILL.md must stay concise"
    assert (plugin.directory / "server.py").is_file()
    assert (plugin.directory / "__init__.py").is_file()
    for tile in plugin.manifest.get("surfaces", {}).get("tiles", []):
        name = tile["resourceUri"].rsplit("/", 1)[1]
        assert (plugin.directory / "ui" / f"{name}.html").is_file()
    assert list((plugin.directory / "ui").glob("*.html"))


def test_voice_tools_of_stateful_plugins() -> None:
    timer = SOURCE.get("builtin.timer")
    assert timer is not None
    voice = {name for name, policy in timer.manifest["tools"].items() if policy.get("voice")}
    assert {"start", "get", "cancel"} <= voice
