"""Installations, plugin state and dashboard: versions, idempotency, tenant isolation."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from httpx import AsyncClient

from tests.workspace.harness import Harness, android_login, key, requires_postgres

pytestmark = requires_postgres

TIMER = {"plugin_id": "mia.timer", "version": "1.0.0", "tools_digest": "sha256:timer-tools-v1"}


async def install(client: AsyncClient, headers: dict[str, str], body: dict[str, Any]) -> Any:
    response = await client.post("/v1/installations", headers={**headers, **key()}, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def tile(installation_id: str, position: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "installation_id": installation_id,
        "tile_id": "main",
        "size": "2x1",
        "position": position,
        **extra,
    }


async def test_catalog_install_pins_version_digest_and_grants(harness: Harness) -> None:
    async with harness.client() as client:
        auth = await android_login(client, "sub-install")
        unknown = await client.post(
            "/v1/installations",
            headers={**auth, **key()},
            json={**TIMER, "plugin_id": "mia.unknown"},
        )
        assert unknown.status_code == 404
        mismatch = await client.post(
            "/v1/installations",
            headers={**auth, **key()},
            json={**TIMER, "tools_digest": "sha256:other"},
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "tools_digest_mismatch"

        timer = await install(client, auth, TIMER)
        assert timer["status"] == "active"
        assert timer["grants"] == ["state.read", "state.write"]
        assert timer["source"] == "catalog" and timer["state_version"] == 0
        again = await install(client, auth, TIMER)
        assert again["id"] == timer["id"]
        linear = await install(
            client,
            auth,
            {
                "plugin_id": "team.linear",
                "version": "2.1.0",
                "tools_digest": "sha256:linear-tools-v2",
            },
        )
        assert linear["status"] == "needs_connection"
        custom = await install(client, auth, {"custom_mcp_url": "https://mcp.example.test/mcp"})
        assert custom["status"] == "needs_consent" and custom["source"] == "custom"
        assert custom["custom_url"] == "https://mcp.example.test/mcp"
        bad_url = await client.post(
            "/v1/installations",
            headers={**auth, **key()},
            json={"custom_mcp_url": "https://user:pw@mcp.example.test/"},
        )
        assert bad_url.status_code == 422
        missing_key = await client.post("/v1/installations", headers=auth, json=TIMER)
        assert missing_key.status_code == 422

        listed = await client.get("/v1/installations", headers=auth)
        assert [item["plugin_id"] for item in listed.json()["items"]] == [
            "mia.timer",
            "team.linear",
            custom["plugin_id"],
        ]


async def test_installation_patch_uses_expected_version(harness: Harness) -> None:
    async with harness.client() as client:
        auth = await android_login(client, "sub-patch")
        timer = await install(client, auth, TIMER)
        path = f"/v1/installations/{timer['id']}"
        patched = await client.patch(
            path, headers={**auth, **key()}, json={"expected_version": 0, "status": "disabled"}
        )
        assert patched.status_code == 200
        assert patched.json()["status"] == "disabled" and patched.json()["state_version"] == 1
        stale = await client.patch(
            path, headers={**auth, **key()}, json={"expected_version": 0, "status": "active"}
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "version_conflict"
        assert stale.json()["error"]["details"] == {"current_version": 1}


async def test_plugin_state_compare_and_set(harness: Harness) -> None:
    async with harness.client() as client:
        auth = await android_login(client, "sub-state")
        timer = await install(client, auth, TIMER)
        path = f"/v1/installations/{timer['id']}/state"
        empty = await client.get(path, headers=auth)
        assert empty.json() == {"plugin_id": "mia.timer", "data": {}, "state_version": 0}
        created = await client.put(
            path, headers={**auth, **key()}, json={"expected_version": 0, "data": {"left": 60}}
        )
        assert created.json()["state_version"] == 1
        conflict = await client.put(
            path, headers={**auth, **key()}, json={"expected_version": 0, "data": {"left": 1}}
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["details"]["current_version"] == 1
        updated = await client.put(
            path, headers={**auth, **key()}, json={"expected_version": 1, "data": {"left": 30}}
        )
        assert updated.json()["state_version"] == 2
        assert (await client.get(path, headers=auth)).json()["data"] == {"left": 30}
        too_big = await client.put(
            path,
            headers={**auth, **key()},
            json={"expected_version": 2, "data": {"blob": "x" * 70_000}},
        )
        assert too_big.status_code == 422


async def test_dashboard_expected_version_and_mobile_layout(harness: Harness) -> None:
    async with harness.client() as client:
        auth = await android_login(client, "sub-dashboard")
        timer = await install(client, auth, TIMER)
        initial = await client.get("/v1/dashboard", headers=auth)
        assert initial.json() == {"expected_version": 0, "tiles": []}

        first = tile(timer["id"], mobile={"size": "4x2", "position": 3}, settings={"theme": "x"})
        saved = await client.put(
            "/v1/dashboard",
            headers={**auth, **key()},
            json={"expected_version": 0, "tiles": [first]},
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["expected_version"] == 1
        assert body["tiles"][0]["mobile"] == {"size": "4x2", "position": 3}
        assert body["tiles"][0]["settings"] == {"theme": "x"}

        stale = await client.put(
            "/v1/dashboard", headers={**auth, **key()}, json={"expected_version": 0, "tiles": []}
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == {
            "code": "version_conflict",
            "message": "The resource was changed by another request.",
            "request_id": stale.headers["x-request-id"],
            "retryable": False,
            "details": {"current_version": 1},
        }

        moved = {**first, "position": 5}
        del moved["mobile"]
        resaved = await client.put(
            "/v1/dashboard",
            headers={**auth, **key()},
            json={"expected_version": 1, "tiles": [moved]},
        )
        assert resaved.json()["tiles"][0]["mobile"] == {"size": "2x1", "position": 5}
        assert resaved.json()["tiles"][0]["state_version"] == 0

        tile_path = f"/v1/dashboard/tiles/{first['id']}"
        settings = await client.patch(
            tile_path, headers={**auth, **key()}, json={"expected_version": 0, "settings": {"a": 1}}
        )
        assert settings.status_code == 200 and settings.json()["state_version"] == 1
        stale_tile = await client.patch(
            tile_path, headers={**auth, **key()}, json={"expected_version": 0, "settings": {}}
        )
        assert stale_tile.status_code == 409


async def test_concurrent_dashboard_writes_have_one_winner(harness: Harness) -> None:
    async with harness.client() as client:
        auth = await android_login(client, "sub-race")
        timer = await install(client, auth, TIMER)
        attempts = [
            client.put(
                "/v1/dashboard",
                headers={**auth, **key()},
                json={"expected_version": 0, "tiles": [tile(timer["id"], position)]},
            )
            for position in range(5)
        ]
        results = await asyncio.gather(*attempts)
        assert sorted(response.status_code for response in results) == [200, 409, 409, 409, 409]
        final = await client.get("/v1/dashboard", headers=auth)
        assert final.json()["expected_version"] == 1 and len(final.json()["tiles"]) == 1


async def test_idempotency_key_replays_and_rejects_reuse(harness: Harness) -> None:
    async with harness.client() as client:
        auth = await android_login(client, "sub-idem")
        timer = await install(client, auth, TIMER)
        headers = {**auth, **key()}
        body = {"expected_version": 0, "tiles": [tile(timer["id"])]}
        first = await client.put("/v1/dashboard", headers=headers, json=body)
        retry = await client.put("/v1/dashboard", headers=headers, json=body)
        assert first.status_code == retry.status_code == 200
        assert first.json() == retry.json()
        reused = await client.put(
            "/v1/dashboard", headers=headers, json={"expected_version": 1, "tiles": []}
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "idempotency_conflict"
        assert (await client.get("/v1/dashboard", headers=auth)).json()["expected_version"] == 1


async def test_uninstall_removes_tiles_bumps_layout_and_optionally_data(harness: Harness) -> None:
    async with harness.client() as client:
        auth = await android_login(client, "sub-uninstall")
        timer = await install(client, auth, TIMER)
        state_path = f"/v1/installations/{timer['id']}/state"
        await client.put(
            state_path, headers={**auth, **key()}, json={"expected_version": 0, "data": {"k": 1}}
        )
        await client.put(
            "/v1/dashboard",
            headers={**auth, **key()},
            json={"expected_version": 0, "tiles": [tile(timer["id"])]},
        )
        removed = await client.delete(f"/v1/installations/{timer['id']}", headers={**auth, **key()})
        assert removed.status_code == 204
        dashboard = (await client.get("/v1/dashboard", headers=auth)).json()
        assert dashboard == {"expected_version": 2, "tiles": []}

        reinstalled = await install(client, auth, TIMER)
        kept = await client.get(f"/v1/installations/{reinstalled['id']}/state", headers=auth)
        assert kept.json()["data"] == {"k": 1}
        purge = await client.delete(
            f"/v1/installations/{reinstalled['id']}",
            headers={**auth, **key()},
            params={"delete_data": "true"},
        )
        assert purge.status_code == 204
        fresh = await install(client, auth, TIMER)
        cleared = await client.get(f"/v1/installations/{fresh['id']}/state", headers=auth)
        assert cleared.json()["state_version"] == 0
        gone = await client.delete(f"/v1/installations/{timer['id']}", headers={**auth, **key()})
        assert gone.status_code == 404


async def test_other_account_sees_and_changes_nothing(harness: Harness) -> None:
    async with harness.client() as client:
        owner = await android_login(client, "sub-owner")
        other = await android_login(client, "sub-intruder")
        timer = await install(client, owner, TIMER)
        owner_tile = tile(timer["id"])
        await client.put(
            "/v1/dashboard",
            headers={**owner, **key()},
            json={"expected_version": 0, "tiles": [owner_tile]},
        )
        state_path = f"/v1/installations/{timer['id']}/state"
        await client.put(
            state_path, headers={**owner, **key()}, json={"expected_version": 0, "data": {"s": 1}}
        )

        assert (await client.get("/v1/installations", headers=other)).json() == {"items": []}
        assert (await client.get("/v1/dashboard", headers=other)).json() == {
            "expected_version": 0,
            "tiles": [],
        }
        assert (await client.get("/v1/workspace", headers=other)).json()["id"] != (
            await client.get("/v1/workspace", headers=owner)
        ).json()["id"]
        installation_path = f"/v1/installations/{timer['id']}"
        foreign = [
            await client.get(installation_path, headers=other),
            await client.patch(
                installation_path,
                headers={**other, **key()},
                json={"expected_version": 0, "status": "disabled"},
            ),
            await client.delete(installation_path, headers={**other, **key()}),
            await client.get(state_path, headers=other),
            await client.put(
                state_path, headers={**other, **key()}, json={"expected_version": 1, "data": {}}
            ),
            await client.patch(
                f"/v1/dashboard/tiles/{owner_tile['id']}",
                headers={**other, **key()},
                json={"expected_version": 0, "settings": {"x": 1}},
            ),
            await client.put(
                "/v1/dashboard",
                headers={**other, **key()},
                json={"expected_version": 0, "tiles": [tile(timer["id"])]},
            ),
        ]
        assert [response.status_code for response in foreign] == [404] * len(foreign)
        assert all(r.json()["error"]["code"] == "resource_not_found" for r in foreign)

        other_timer = await install(client, other, TIMER)
        hijack = await client.put(
            "/v1/dashboard",
            headers={**other, **key()},
            json={
                "expected_version": 0,
                "tiles": [{**owner_tile, "installation_id": other_timer["id"]}],
            },
        )
        assert hijack.status_code == 422

        # The same Idempotency-Key in another workspace is independent.
        shared = {"Idempotency-Key": "shared-key-0000000001"}
        empty = {"expected_version": 0, "tiles": []}
        assert (
            await client.put("/v1/dashboard", headers={**other, **shared}, json=empty)
        ).status_code == 200
        assert (
            await client.put("/v1/dashboard", headers={**owner, **shared}, json=empty)
        ).status_code == 409

        owner_view = (await client.get("/v1/dashboard", headers=owner)).json()
        assert [item["id"] for item in owner_view["tiles"]] == [owner_tile["id"]]
        assert (await client.get(state_path, headers=owner)).json()["data"] == {"s": 1}
        assert (await client.get(installation_path, headers=owner)).json()["status"] == "active"


async def test_unauthenticated_requests_are_refused(harness: Harness) -> None:
    async with harness.client() as client:
        for method, path in (
            ("GET", "/v1/workspace"),
            ("GET", "/v1/installations"),
            ("GET", "/v1/dashboard"),
        ):
            response = await client.request(method, path)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "authentication_required"
        bad = await client.get("/v1/dashboard", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
