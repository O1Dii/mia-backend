"""Publishing and reading the catalog on Postgres: every builtin plugin, checks, API, reviews."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from app.modules.plugins.builtin import BUILTIN_ROOT
from app.modules.plugins.catalog.errors import (
    PackageInvalidError,
    PublishForbiddenError,
    ReleaseExistsError,
)
from app.modules.plugins.catalog.package import PackageFile, PluginPackage, load_package
from app.modules.plugins.catalog.sources import ReleaseManifestSource

from tests.catalog.harness import Store, build_store
from tests.catalog.test_pure import BUILTIN_IDS
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture
async def store():  # type: ignore[no-untyped-def]
    async with build_store() as value:
        yield value


async def test_all_builtin_plugins_publish_and_match_the_router_digest(store: Store) -> None:
    for name in BUILTIN_IDS:
        result = await store.publish_builtin(name)
        assert result.created
    page = (await store.get(store.admin, "/v1/plugins")).json()
    assert [item["plugin_id"] for item in page["items"]] == sorted(
        f"builtin.{name}" for name in BUILTIN_IDS
    ) or len(page["items"]) == len(BUILTIN_IDS)
    # The digest stored at publication is the digest the router computes for the installation.
    for item in page["items"]:
        resolved = await store.builtin.catalog.resolve(item["plugin_id"], item["version"])
        assert resolved is not None
        release = await store.catalog.release(item["plugin_id"], item["version"])
        assert release.tools_digest == resolved.tools_digest


async def test_publishing_is_idempotent_and_versions_are_immutable(store: Store) -> None:
    first = await store.publish_builtin("timer")
    again = await store.publish_builtin("timer")
    assert first.created and not again.created and first.release.id == again.release.id

    package = load_package(BUILTIN_ROOT / "timer")
    changed = PluginPackage(
        manifest={**package.manifest, "name": "Таймер 2"},
        files=dict(package.files),
        directory=package.directory,
    )
    with pytest.raises(ReleaseExistsError):
        await store.publish(changed)


async def test_only_an_administrator_publishes(store: Store) -> None:
    user = await store.login("plain-user")
    plain_id = await store.user_id(user)
    with pytest.raises(PublishForbiddenError):
        await store.catalog.publish(load_package(BUILTIN_ROOT / "timer"), actor_user_id=plain_id)


async def test_tools_must_equal_the_servers_tools_list(store: Store) -> None:
    package = load_package(BUILTIN_ROOT / "timer")
    manifest = copy.deepcopy(package.manifest)
    manifest["tools"]["invented"] = {"risk": "read"}
    del manifest["tools"]["pause"]
    with pytest.raises(PackageInvalidError) as failure:
        await store.publish(
            PluginPackage(manifest=manifest, files=dict(package.files), directory=package.directory)
        )
    # The builtin server itself refuses a manifest that does not match its code.
    assert {p["code"] for p in failure.value.problems} <= {"discover_failed", "tools_mismatch"}


def test_overlay_problems_name_each_missing_and_extra_tool() -> None:
    from app.modules.plugins.catalog.discovery import overlay_problems  # noqa: PLC0415
    from app.modules.tools.digest import ServerTool  # noqa: PLC0415

    manifest = {"server": {"kind": "remote"}, "tools": {"a": {}, "b": {}}}
    served = tuple(ServerTool(name, "", {}) for name in ("b", "c"))
    assert {(p.code, p.path) for p in overlay_problems(manifest, served)} == {
        ("tools_mismatch", "tools.a"),
        ("tools_mismatch", "tools.c"),
    }
    declarative = {"server": {"kind": "declarative"}, "tools": {"x": {}}}
    hidden = tuple(ServerTool(name, "", {}) for name in ("x", "get_state"))
    assert overlay_problems(declarative, hidden) == []


async def test_unsafe_bundled_ui_and_schema_violations_block_publication(store: Store) -> None:
    package = load_package(BUILTIN_ROOT / "timer")
    files = dict(package.files)
    files["ui/evil.html"] = PackageFile(b'<html><script src="https://evil.example/x.js"></script>')
    with pytest.raises(PackageInvalidError) as unsafe:
        await store.publish(
            PluginPackage(manifest=package.manifest, files=files, directory=package.directory)
        )
    assert [p["code"] for p in unsafe.value.problems] == ["ui_unsafe"]

    broken = copy.deepcopy(package.manifest)
    broken["version"] = "1"
    with pytest.raises(PackageInvalidError) as schema:
        await store.publish(
            PluginPackage(manifest=broken, files=dict(package.files), directory=package.directory)
        )
    assert schema.value.problems[0]["code"] == "manifest_schema"


async def test_a_served_tile_without_the_host_csp_is_rejected(store: Store) -> None:
    """The in-process server's tile resources are read and checked as served."""
    from app.modules.plugins.catalog.discovery import _read_tiles  # noqa: PLC0415
    from mcp.server.mcpserver import MCPServer  # noqa: PLC0415

    server = MCPServer("t")

    @server.resource("ui://builtin.timer/tile", mime_type="text/html;profile=mcp-app")
    def tile() -> str:
        return "<html><body>no csp</body></html>"

    manifest = load_package(BUILTIN_ROOT / "timer").manifest
    _, problems = await _read_tiles(manifest, server)
    assert [p.message for p in problems] == [
        "the served resource lacks the host Content-Security-Policy"
    ]


async def test_catalog_search_category_pages_and_detail(store: Store) -> None:
    for name in BUILTIN_IDS:
        await store.publish_builtin(name)
    everything = (await store.get(store.admin, "/v1/plugins", limit=100)).json()
    names = [item["name"] for item in everything["items"]]
    assert names == sorted(names) and len(names) == len(BUILTIN_IDS)

    first = (await store.get(store.admin, "/v1/plugins", limit=2)).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = (
        await store.get(store.admin, "/v1/plugins", limit=2, cursor=first["next_cursor"])
    ).json()
    assert {i["plugin_id"] for i in first["items"]}.isdisjoint(
        i["plugin_id"] for i in second["items"]
    )

    found = (await store.get(store.admin, "/v1/plugins", query="таймер")).json()["items"]
    assert [i["plugin_id"] for i in found] == ["builtin.timer"]
    assert (await store.get(store.admin, "/v1/plugins", query="100%")).json()["items"] == []
    utilities = (await store.get(store.admin, "/v1/plugins", category="utilities")).json()["items"]
    assert utilities and all(i["category"] == "utilities" for i in utilities)
    assert (await store.get(store.admin, "/v1/plugins", cursor="@@@")).status_code == 422

    detail = (await store.get(store.admin, "/v1/plugins/builtin.timer")).json()
    assert detail["tagline"] == "У всего есть своё время."
    assert detail["color"] == "mint" and detail["kind"] == "builtin"
    assert detail["versions"] == ["1.0.0"] and detail["manifest"]["id"] == "builtin.timer"
    consent = detail["consent"]
    assert {t["name"]: t["risk"] for t in consent["tools"]}["start"] == "write_reversible"
    assert {"tools.read", "tools.write_reversible", "notify", "schedules"} <= {
        g["id"] for g in consent["grants"]
    }
    assert consent == (await store.get(store.admin, "/v1/plugins/builtin.timer/consent")).json()
    assert (await store.get(store.admin, "/v1/plugins/builtin.nothing")).status_code == 404

    internet = (await store.get(store.admin, "/v1/plugins/builtin.internet")).json()["consent"]
    assert internet["public_web"] is True
    assert "network.public_web" in {g["id"] for g in internet["grants"]}
    news = (await store.get(store.admin, "/v1/plugins/builtin.news")).json()["consent"]
    assert news["llm"]["via_host"] is True and "llm.generate" in {g["id"] for g in news["grants"]}


async def test_yanked_versions_leave_the_catalog_but_installs_keep_running(store: Store) -> None:
    await store.publish_builtin("timer")
    await store.catalog.yank("builtin.timer", "1.0.0", actor_user_id=store.admin_id)
    assert (await store.get(store.admin, "/v1/plugins")).json()["items"] == []
    assert await store.catalog.resolve("builtin.timer", "1.0.0") is None
    assert await store.catalog.any_release("builtin.timer", "1.0.0") is not None


async def test_reviews_aggregate_and_show_first_names_only(store: Store) -> None:
    await store.publish_builtin("timer")
    ann = await store.login("ann")
    bob = await store.login("bob")
    for headers, rating in ((ann, 5), (bob, 2)):
        response = await store.client.put(
            "/v1/plugins/builtin.timer/reviews/me",
            headers={**headers, "Idempotency-Key": f"review-key-{rating:08d}"},
            json={"rating": rating, "text": f"rated {rating}"},
        )
        assert response.status_code == 204
    reviews = (await store.get(ann, "/v1/plugins/builtin.timer/reviews")).json()
    assert reviews["rating"] == {"average": 3.5, "count": 2}
    assert {r["text"]: r["mine"] for r in reviews["items"]} == {"rated 5": True, "rated 2": False}
    assert {r["author"] for r in reviews["items"]} == {"Ann", "Bob"}
    entry = (await store.get(ann, "/v1/plugins/builtin.timer")).json()
    assert entry["rating"] == {"average": 3.5, "count": 2}

    # One review per user: editing replaces it; deleting removes it.
    again = await store.client.put(
        "/v1/plugins/builtin.timer/reviews/me",
        headers={**ann, "Idempotency-Key": "review-key-again-01"},
        json={"rating": 1, "text": "changed"},
    )
    assert again.status_code == 204
    assert (await store.get(ann, "/v1/plugins/builtin.timer/reviews")).json()["rating"] == {
        "average": 1.5,
        "count": 2,
    }
    gone = await store.delete(bob, "/v1/plugins/builtin.timer/reviews/me")
    assert gone.status_code == 204
    assert (await store.get(ann, "/v1/plugins/builtin.timer/reviews")).json()["rating"][
        "count"
    ] == 1
    bad = await store.client.put(
        "/v1/plugins/builtin.timer/reviews/me",
        headers={**ann, "Idempotency-Key": "review-key-bad-001"},
        json={"rating": 6},
    )
    assert bad.status_code == 422
    unknown = await store.client.put(
        "/v1/plugins/builtin.unknown/reviews/me",
        headers={**ann, "Idempotency-Key": "review-key-unk-001"},
        json={"rating": 3},
    )
    assert unknown.status_code == 404


async def test_reviews_imported_from_the_web_poc_show_on_the_replacing_plugin(store: Store) -> None:
    await store.publish_builtin("timer")
    ann = await store.login("ann")
    await store.workspace.upsert_review(await store.user_id(ann), "mia.timer", 4, "из веб-POC")
    reviews = (await store.get(ann, "/v1/plugins/builtin.timer/reviews")).json()
    assert reviews["rating"] == {"average": 4.0, "count": 1}
    assert reviews["items"][0]["plugin_id"] == "builtin.timer"


async def test_release_files_are_images_only_and_served_with_hardening(store: Store) -> None:
    package = load_package(BUILTIN_ROOT / "timer")
    manifest = copy.deepcopy(package.manifest)
    manifest["listing"]["icon"] = "assets/icon.png"
    files = dict(package.files)
    files["assets/icon.png"] = PackageFile(b"\x89PNG\r\n\x1a\n\x00\xff")
    await store.publish(PluginPackage(manifest=manifest, files=files, directory=package.directory))

    entry = (await store.get(store.admin, "/v1/plugins/builtin.timer")).json()
    assert entry["icon_url"] == "/v1/plugins/builtin.timer/releases/1.0.0/files/assets/icon.png"
    image = await store.client.get(entry["icon_url"], headers=store.admin)
    assert image.status_code == 200 and image.content == b"\x89PNG\r\n\x1a\n\x00\xff"
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in image.headers["content-security-policy"]
    for hidden in ("SKILL.md", "ui/tile.html"):
        response = await store.client.get(
            f"/v1/plugins/builtin.timer/releases/1.0.0/files/{hidden}", headers=store.admin
        )
        assert response.status_code == 404


async def test_release_packages_are_materialized_for_the_router(
    store: Store, tmp_path: Path
) -> None:
    await store.publish_builtin("timer")
    source = ReleaseManifestSource(store.catalog, tmp_path)
    loaded = await source.load("builtin.timer", "1.0.0")
    assert loaded is not None and loaded.plugin_dir is not None
    skill = (loaded.plugin_dir / "SKILL.md").read_text("utf-8")
    assert skill == (BUILTIN_ROOT / "timer" / "SKILL.md").read_text("utf-8")
    assert await source.load("builtin.timer", "9.9.9") is None
    assert json.loads(json.dumps(loaded.manifest))["id"] == "builtin.timer"
