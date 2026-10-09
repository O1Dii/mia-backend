"""The implemented store routes and ``contracts/mvp/v1/openapi.yaml`` describe the same API."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from tests.catalog.harness import build_store
from tests.runs.conftest import requires_postgres

OPENAPI = Path(__file__).resolve().parents[2] / "contracts/mvp/v1/openapi.yaml"
PREFIXES = (
    "/v1/plugins",
    "/v1/installations",
    "/v1/connections",
    "/v1/dashboard",
    "/v1/workspace",
    "/v1/auth/web",
)
METHODS = {"get", "post", "put", "patch", "delete"}


def contract() -> set[tuple[str, str]]:
    spec: dict[str, Any] = yaml.safe_load(OPENAPI.read_text("utf-8"))
    return {
        (method.upper(), re.sub(r"\{[^}]+\}", "{}", path))
        for path, item in spec["paths"].items()
        if path.startswith(PREFIXES)
        for method in item
        if method in METHODS
    }


@requires_postgres
async def test_every_store_route_is_in_the_contract_and_the_other_way_round() -> None:
    async with build_store() as store:
        schema = store.app.openapi()["paths"]
    implemented = {
        (method.upper(), re.sub(r"\{[^}:]+(:path)?\}", "{}", path))
        for path, item in schema.items()
        if path.startswith(PREFIXES)
        for method in item
        if method in METHODS
    }
    # The OAuth redirect and the client metadata document are served but kept out of the
    # generated schema (include_in_schema=False); the contract still lists them.
    hidden = {
        ("GET", "/v1/connections/callback"),
        ("GET", "/v1/connections/client-metadata.json"),
    }
    # M24: the WebView hand-off is mounted by install_runtime_api; tests/web_handoff covers it.
    mounted_elsewhere = {
        ("GET", "/v1/auth/web/handoff/redeem"),
        ("POST", "/v1/auth/web/handoff"),
    }
    assert implemented | hidden | mounted_elsewhere == contract()
    assert not implemented & hidden
    assert not implemented & mounted_elsewhere
