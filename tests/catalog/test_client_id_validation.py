"""M51-R1: ``server.clientId`` publication rules (no database)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from app.modules.plugins.catalog.validation import schema_problems, semantic_problems

BASE: dict[str, Any] = {
    "schemaVersion": "mia.plugin/1",
    "id": "team.maps",
    "version": "1.0.0",
    "name": "Maps",
    "listing": {"description": "Mind maps of your texts", "category": "knowledge"},
    "server": {"kind": "remote", "url": "https://maps.example/mcp", "auth": "mia_token"},
    "tools": {"list_maps": {"risk": "read"}},
    "permissions": {
        "network": ["maps.example"],
        "llm": {"viaHost": False},
        "memory": [],
        "notify": False,
    },
}


def manifest(**server: Any) -> dict[str, Any]:
    body = copy.deepcopy(BASE)
    body["server"].update(server)
    return {**body, "server": {k: v for k, v in body["server"].items() if v is not None}}


def paths(body: dict[str, Any], *, loopback: bool = False) -> list[str]:
    assert schema_problems(body) == []
    return [p.path for p in semantic_problems(body, allow_loopback_client=loopback)]


def test_mia_token_needs_an_https_client_id_on_a_declared_host() -> None:
    assert paths(manifest(clientId="https://maps.example/oauth/client.json")) == []
    assert paths(manifest()) == ["server.clientId"]
    assert paths(manifest(clientId="https://evil.example/client.json")) == ["server.clientId"]
    assert paths(manifest(clientId="http://maps.example/client.json")) == ["server.clientId"]
    assert paths(manifest(clientId="https://maps.example/c.json?x=1")) == ["server.clientId"]


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:9000"])
def test_loopback_client_id_only_when_loopback_clients_are_allowed(host: str) -> None:
    body = manifest(clientId=f"http://{host}/api/auth/mia/client-metadata.json")
    assert paths(body) == ["server.clientId"]
    assert paths(body, loopback=True) == []


def test_client_id_is_only_for_mia_token() -> None:
    body = manifest(auth="oauth", clientId="https://maps.example/oauth/client.json")
    assert paths(body) == ["server.clientId"]
    assert paths(manifest(auth="oauth")) == []


def test_schema_refuses_a_client_id_that_is_not_a_web_url() -> None:
    assert schema_problems(manifest(clientId="ftp://maps.example/c.json"))
    assert schema_problems(manifest(clientId="https://x/" + "a" * 260))
