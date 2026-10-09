"""Pure checks of M50 building blocks (no database)."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from app.core.config import Settings
from app.main import create_app
from app.modules.oauth import secrets as creds
from app.modules.oauth.cimd import (
    ClientMetadataError,
    check_client_id,
    check_redirect_uri,
    parse_document,
)
from app.modules.oauth.idtoken import IdTokenSigner
from app.modules.oauth.scopes import (
    InvalidScopeError,
    describe,
    parse_scope,
    plugins_allowed,
    readable_categories,
)
from app.modules.oauth.service import with_query
from fastapi.testclient import TestClient
from pydantic import ValidationError

OPENAPI = Path(__file__).resolve().parents[2] / "contracts/mvp/v1/openapi.yaml"
M50_PREFIXES = ("/v1/oauth", "/v1/apps", "/v1/chat/completions", "/v1/responses", "/v1/models")


def test_scope_vocabulary() -> None:
    assert parse_scope("openid  offline_access llm.generate openid") == ("openid", "llm.generate")
    assert parse_scope("memory.read:work tools.use:team.linear tools.use") == (
        "memory.read:work",
        "tools.use:team.linear",
        "tools.use",
    )
    for bad in ("", "offline_access", "memory.read:passwords", "tools.use:X", "admin", "openid*"):
        with pytest.raises(InvalidScopeError):
            parse_scope(bad)
    scopes = frozenset({"memory.read:work", "memory.read:home", "tools.use:team.linear"})
    assert readable_categories(scopes) == {"work", "home"}
    assert plugins_allowed(scopes) == frozenset({"team.linear"})
    assert plugins_allowed(frozenset({"tools.use"})) is None
    assert describe("memory.read:health").sensitive
    assert describe("tools.use:team.linear").title == "Плагин team.linear"


def test_pkce_is_s256_only_and_constant_time() -> None:
    verifier = "a" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    challenge = challenge.rstrip("=")
    assert creds.verify_pkce(verifier, challenge)
    assert not creds.verify_pkce(verifier, verifier)  # "plain" never matches
    assert not creds.verify_pkce("short", challenge)
    assert not creds.verify_pkce("a" * 129, challenge)
    assert creds.valid_challenge(challenge) and not creds.valid_challenge(challenge + "=")


def test_tokens_have_kind_prefixes_and_are_hashed() -> None:
    token = creds.new_secret("access")
    assert creds.looks_like(token, "access") and not creds.looks_like(token, "refresh")
    assert creds.digest(token) != token and len(creds.digest(token)) == 64
    assert not creds.looks_like("mia_at_" + "!" * 43, "access")


@pytest.mark.parametrize(
    "client_id",
    [
        "http://a.example/c.json",
        "https://a.example",
        "https://a.example/",
        "https://a.example/c.json?x=1",
        "https://a.example/c.json#f",
        "https://u:p@a.example/c.json",
        "https://a.example/../c.json",
        "https://a.example/" + "x" * 300,
    ],
)
def test_client_id_shape(client_id: str) -> None:
    with pytest.raises(ClientMetadataError):
        check_client_id(client_id)


def test_loopback_client_ids_only_when_allowed() -> None:
    with pytest.raises(ClientMetadataError):
        check_client_id("http://localhost:8001/client.json")
    check_client_id("http://localhost:8001/client.json", allow_loopback=True)
    with pytest.raises(ClientMetadataError):
        check_client_id("http://example.com/client.json", allow_loopback=True)
    with pytest.raises(ValidationError):
        Settings(
            app_env="production",
            google_client_id="x.apps.googleusercontent.com",
            auth_jwt_secret="p" * 40,
            credentials_master_keys="k1:" + "A" * 43,
            oauth_allow_loopback_clients=True,
        )


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("https://app.example/cb", True),
        ("http://127.0.0.1:8765/cb", True),
        ("http://[::1]/cb", True),
        ("com.example.app:/oauth", True),
        ("http://app.example/cb", False),
        ("javascript:alert(1)", False),
        ("data:text/html,x", False),
        ("https://app.example/cb#frag", False),
        ("myapp:/cb", False),
    ],
)
def test_redirect_uri_rules(uri: str, ok: bool) -> None:
    if ok:
        check_redirect_uri(uri)
    else:
        with pytest.raises(ClientMetadataError):
            check_redirect_uri(uri)


def test_document_drops_non_https_links() -> None:
    meta = parse_document(
        "https://a.example/c.json",
        b'{"client_id":"https://a.example/c.json","redirect_uris":["https://a.example/cb"],'
        b'"logo_uri":"http://a.example/logo.png","client_uri":"https://a.example"}',
    )
    assert meta.client_name == "a.example" and meta.logo_uri is None
    assert meta.client_uri == "https://a.example"


def test_id_token_key_is_stable_and_secret_bound() -> None:
    one, two = IdTokenSigner(b"s" * 40), IdTokenSigner(b"s" * 40)
    other = IdTokenSigner(b"t" * 40)
    assert one.jwks() == two.jwks() and one.jwks() != other.jwks()
    assert "d" not in one.jwks()["keys"][0]  # never the private part
    token = one.id_token(
        issuer="https://mia.test", subject="s", audience="a", auth_time=1, nonce=None, ttl_s=60
    )
    assert token.count(".") == 2


def test_with_query_keeps_the_redirect_query() -> None:
    assert with_query("https://a.example/cb?x=1", {"code": "c d"}) == (
        "https://a.example/cb?x=1&code=c+d"
    )


def _contract() -> set[tuple[str, str]]:
    spec: dict[str, Any] = yaml.safe_load(OPENAPI.read_text("utf-8"))
    return {
        (method.upper(), re.sub(r"\{[^}]+\}", "{}", path))
        for path, item in spec["paths"].items()
        if path.startswith(M50_PREFIXES)
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }


def test_production_app_mounts_m50_and_matches_the_contract() -> None:
    app = create_app(Settings(app_env="test"))
    paths = app.openapi()["paths"]
    implemented = {
        (method.upper(), re.sub(r"\{[^}]+\}", "{}", path))
        for path, item in paths.items()
        if path.startswith(M50_PREFIXES)
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert implemented == _contract()
    client = TestClient(app, raise_server_exceptions=False)
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["token_endpoint"] == "http://localhost:8000/oauth/token"
    assert client.get("/oauth/jwks.json").json()["keys"][0]["alg"] == "ES256"
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
    assert client.post("/mcp", json={}).status_code == 401
    assert client.get("/oauth/userinfo").status_code == 401
    assert app.state.oauth.service.config.consent_url == "http://localhost:8000/oauth.html"
