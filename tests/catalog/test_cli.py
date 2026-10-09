"""``python -m cli.mia_plugin``: validation, publishing, permissions and the real entry point."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.modules.plugins.builtin import BUILTIN_ROOT
from app.modules.plugins.catalog.models import PluginRelease
from cli.mia_plugin import run
from sqlalchemy import select

from tests.catalog.harness import Store, build_store
from tests.catalog.test_pure import BUILTIN_IDS, linear
from tests.runs.conftest import requires_postgres

ROOT = Path(__file__).resolve().parents[2]


async def invoke(*argv: str, **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
    out = io.StringIO()
    code = await run(list(argv), out=out, settings=Settings(app_env="test"), **kwargs)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


async def test_validate_accepts_builtin_packages_with_discovery() -> None:
    code, lines = await invoke("validate", str(BUILTIN_ROOT / "timer"), "--discover")
    assert code == 0
    assert lines[-1] == {"plugin_id": "builtin.timer", "valid": True, "version": "1.0.0"}
    assert lines[0]["tools"] == ["cancel", "get", "pause", "resume", "start"]
    assert lines[0]["tools_digest"].startswith("sha256:")


async def test_validate_reports_each_problem_and_fails(tmp_path: Path) -> None:
    manifest = linear()
    manifest["tools"]["BadName"] = {"risk": "read"}
    package = tmp_path / "bad"
    package.mkdir()
    (package / "mia.json").write_text(json.dumps(manifest), "utf-8")
    code, lines = await invoke("validate", str(package))
    assert code == 1
    assert lines[-1]["valid"] is False
    assert any(line.get("problem", {}).get("code") == "manifest_schema" for line in lines)


async def test_unreadable_packages_fail_cleanly(tmp_path: Path) -> None:
    code, lines = await invoke("validate", str(tmp_path / "missing"))  # PackageReadError
    assert code == 1 and lines == [{"error": "mia.json is missing"}]


async def test_publishing_needs_an_administrator_id() -> None:
    code, lines = await invoke("publish", str(BUILTIN_ROOT / "timer"))
    assert code == 2 and "--as-user" in lines[0]["error"]


@requires_postgres
async def test_publish_is_idempotent_forbidden_for_others_and_yankable() -> None:
    async with build_store() as store:
        factory = _factory(store)
        admin = ["--as-user", store.admin_id]
        timer = str(BUILTIN_ROOT / "timer")
        code, lines = await invoke("publish", timer, *admin, service_factory=factory)
        assert code == 0 and lines[0]["created"] is True
        assert lines[0]["plugin_id"] == "builtin.timer" and lines[0]["grants"]
        code, again = await invoke("publish", timer, *admin, service_factory=factory)
        assert code == 0 and again[0]["created"] is False
        assert again[0]["tools_digest"] == lines[0]["tools_digest"]

        stranger = await store.user_id(await store.login("stranger"))
        code, refused = await invoke(
            "publish", timer, "--as-user", stranger, service_factory=factory
        )
        assert code == 2 and refused[0]["code"] == "permission_denied"

        code, yanked = await invoke(
            "yank", "builtin.timer", "1.0.0", *admin, service_factory=factory
        )
        assert code == 0 and yanked[0]["status"] == "yanked"
        assert (await store.get(store.admin, "/v1/plugins")).json()["items"] == []
        code, missing = await invoke(
            "yank", "builtin.nope", "1.0.0", *admin, service_factory=factory
        )
        assert code == 2 and missing[0]["code"] == "resource_not_found"


@requires_postgres
async def test_a_package_that_fails_checks_is_not_stored(tmp_path: Path) -> None:
    async with build_store() as store:
        package = tmp_path / "pkg"
        package.mkdir()
        manifest = linear()
        manifest["version"] = "x"
        (package / "mia.json").write_text(json.dumps(manifest), "utf-8")
        code, lines = await invoke(
            "publish", str(package), "--as-user", store.admin_id, service_factory=_factory(store)
        )
        assert code == 1 and any("problem" in line for line in lines)
        async with store.factory() as session:
            assert (await session.scalars(select(PluginRelease))).all() == []


@requires_postgres
async def test_the_real_entry_point_publishes_every_builtin_from_the_environment() -> None:
    async with build_store() as store:
        env = {
            **os.environ,
            "DATABASE_URL": os.environ["MIA_TEST_DATABASE_URL"],
            "STORE_ADMIN_USER_IDS": store.admin_id,
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cli.mia_plugin",
            "publish-builtin",
            "--as-user",
            store.admin_id,
            cwd=ROOT,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        lines = [json.loads(line) for line in stdout.decode().splitlines()]
        assert sorted(line["plugin_id"] for line in lines) == sorted(
            f"builtin.{n}" for n in BUILTIN_IDS
        )
        assert all(line["created"] for line in lines)
        # What the CLI stored is what the store serves, with the showcase data of catalog.ts.
        page = (await store.get(store.admin, "/v1/plugins", limit=100)).json()["items"]
        assert len(page) == len(BUILTIN_IDS)
        assert {i["plugin_id"]: i["tagline"] for i in page}[
            "builtin.focus"
        ] == "Одна задача. Весь фокус."


def _factory(store: Store) -> Any:
    async def close() -> None:
        return None

    return lambda settings: (store.catalog, close)
