"""«Интернет» is available to everyone without an installation (M73)."""

from __future__ import annotations

import uuid
from typing import Any

from app.modules.plugins.builtin import BuiltinManifestSource, make_inprocess_factory
from app.modules.plugins.composition import (
    AlwaysOnInstallations,
    BuiltinPluginCatalog,
    RouterServerFactory,
)
from app.modules.plugins.registry import (
    InstallationRecord,
    PluginRegistry,
    StaticInstallationReader,
)

from tests.plugins.support import make_services

USER = "user-1"


def build(records: list[InstallationRecord]) -> tuple[AlwaysOnInstallations, BuiltinPluginCatalog]:
    source = BuiltinManifestSource()
    catalog = BuiltinPluginCatalog(
        source, RouterServerFactory(make_inprocess_factory(make_services()))
    )
    inner = StaticInstallationReader({USER: records})
    return AlwaysOnInstallations(inner, source, catalog), catalog


def own(plugin_id: str, *, digest: str | None, status: str = "active") -> InstallationRecord:
    return InstallationRecord(
        id=str(uuid.uuid4()),
        workspace_id="ws-1",
        plugin_id=plugin_id,
        version="1.0.0",
        status=status,
        grants=(),
        tools_digest=digest,
        manifest=None,
    )


async def test_virtual_installation_for_a_user_without_installs() -> None:
    reader, catalog = build([])
    (record,) = await reader.installations(user_id=USER, workspace_id=None)
    resolved = await catalog.resolve("builtin.internet", "1.0.0")
    assert resolved is not None
    assert record.id == "system:builtin.internet" and record.status == "active"
    assert record.tools_digest == resolved.tools_digest and record.grants == ()
    registry = PluginRegistry(reader, None)
    (plugin,) = await registry.installed(user_id=USER, workspace_id=None)
    assert plugin.active and set(plugin.manifest.tools) == {"search", "news", "read"}


async def test_virtual_installation_goes_first_and_takes_the_users_workspace() -> None:
    other = own("builtin.notes", digest="sha256:x")
    reader, _ = build([other])
    records = await reader.installations(user_id=USER, workspace_id=None)
    assert [r.plugin_id for r in records] == ["builtin.internet", "builtin.notes"]
    assert records[0].workspace_id == "ws-1"
    scoped = await reader.installations(user_id=USER, workspace_id="ws-9")
    assert scoped[0].workspace_id == "ws-9"


async def test_healthy_own_installation_is_kept_and_stale_one_is_replaced() -> None:
    reader, catalog = build([])
    resolved = await catalog.resolve("builtin.internet", "1.0.0")
    assert resolved is not None
    healthy = own("builtin.internet", digest=resolved.tools_digest)
    kept, _ = build([healthy])
    assert [r.id for r in await kept.installations(user_id=USER, workspace_id=None)] == [healthy.id]

    for stale in (
        own("builtin.internet", digest="sha256:old"),
        own("builtin.internet", digest=resolved.tools_digest, status="needs_consent"),
    ):
        replaced, _ = build([stale])
        records: list[Any] = list(await replaced.installations(user_id=USER, workspace_id=None))
        assert [r.id for r in records] == ["system:builtin.internet"]


async def test_other_users_are_not_affected() -> None:
    reader, _ = build([own("builtin.notes", digest="sha256:x")])
    records = await reader.installations(user_id="someone-else", workspace_id=None)
    assert [r.plugin_id for r in records] == ["builtin.internet"]
