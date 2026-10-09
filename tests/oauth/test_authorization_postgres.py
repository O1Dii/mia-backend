"""OAuth 2.1 conformance of «Войти через Mia» (M50 DoD): PKCE, scopes, revocation, CIMD.

Every test drives the real HTTP endpoints against PostgreSQL; clients are identified by a
Client ID Metadata Document fetched behind the SSRF guard.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app.modules.oauth.models import OAuthCode, OAuthGrant, OAuthToken
from app.modules.oauth.service import utc_now
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sqlalchemy import select

from tests.oauth.harness import (
    CLIENT_ID,
    ISSUER,
    REDIRECT,
    Documents,
    OAuthWorld,
    client_document,
    oauth_world,
    pkce_pair,
)
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture
async def world() -> Any:
    async with oauth_world() as built:
        yield built


def query(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def verify_id_token(token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    header_b64, claims_b64, signature_b64 = token.split(".")
    header = json.loads(_b64(header_b64))
    assert header["alg"] == "ES256"
    key = next(k for k in jwks["keys"] if k["kid"] == header["kid"])
    public = ec.EllipticCurvePublicNumbers(
        int.from_bytes(_b64(key["x"]), "big"), int.from_bytes(_b64(key["y"]), "big"), ec.SECP256R1()
    ).public_key()
    raw = _b64(signature_b64)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))
    return dict(json.loads(_b64(claims_b64)))


async def token_request(http: httpx.AsyncClient, **data: str) -> httpx.Response:
    return await http.post("/oauth/token", data=data)


async def userinfo(http: httpx.AsyncClient, access: str) -> httpx.Response:
    return await http.get("/oauth/userinfo", headers={"Authorization": f"Bearer {access}"})


# -- metadata -----------------------------------------------------------------------


async def test_metadata_advertises_pkce_s256_only_and_cimd(world: OAuthWorld) -> None:
    async with world.http() as http:
        meta = (await http.get("/.well-known/oauth-authorization-server")).json()
        oidc = (await http.get("/.well-known/openid-configuration")).json()
        prm = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert meta == oidc
    assert meta["issuer"] == ISSUER
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["client_id_metadata_document_supported"] is True
    assert meta["authorization_response_iss_parameter_supported"] is True
    assert meta["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert prm == {
        "resource": f"{ISSUER}/mcp",
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
        "scopes_supported": prm["scopes_supported"],
        "resource_name": "Mia",
    }


# -- happy path + OIDC ----------------------------------------------------------------


async def test_code_flow_issues_tokens_and_a_verifiable_pairwise_id_token(
    world: OAuthWorld,
) -> None:
    async with world.http() as http:
        verifier, challenge = pkce_pair()
        started = await world.authorize(
            http, challenge=challenge, extra={"nonce": "n-1"}, scope="openid email llm.generate"
        )
        assert started.status_code == 302
        assert started.headers["location"].startswith("https://mia.test/oauth.html?request=mia_rq_")
        decided = await world.consent(http, started.headers["location"])
        back = query(decided.json()["redirect_to"])
        assert decided.json()["redirect_to"].startswith(REDIRECT + "?")
        assert back["state"] == "st-123" and back["iss"] == ISSUER
        tokens = (
            await token_request(
                http,
                grant_type="authorization_code",
                client_id=CLIENT_ID,
                code=back["code"],
                code_verifier=verifier,
                redirect_uri=REDIRECT,
            )
        ).json()
        jwks = (await http.get("/oauth/jwks.json")).json()
        info = (await userinfo(http, tokens["access_token"])).json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 900
    assert tokens["scope"] == "openid email llm.generate"
    assert tokens["access_token"].startswith("mia_at_")
    assert tokens["refresh_token"].startswith("mia_rt_")
    claims = verify_id_token(tokens["id_token"], jwks)
    assert claims["iss"] == ISSUER and claims["aud"] == CLIENT_ID and claims["nonce"] == "n-1"
    assert claims["email"].endswith("@example.test") and "name" not in claims
    assert claims["sub"] == info["sub"] != str(world.user)  # pairwise, not the internal id
    with pytest.raises(InvalidSignature):
        tampered = tokens["id_token"][:-4] + (
            "AAAA" if not tokens["id_token"].endswith("AAAA") else "BBBB"
        )
        verify_id_token(tampered, jwks)


async def test_credentials_are_stored_only_as_digests(world: OAuthWorld) -> None:
    async with world.http() as http:
        tokens = await world.login(http)
    async with world.factory() as session:
        rows = (await session.scalars(select(OAuthToken))).all()
        codes = (await session.scalars(select(OAuthCode))).all()
    stored = json.dumps(
        [[r.token_hash, r.scopes, r.client_id] for r in rows] + [[c.code_hash] for c in codes]
    )
    assert len(rows) == 2 and all(len(r.token_hash) == 64 for r in rows)
    assert tokens["access_token"] not in stored and tokens["refresh_token"] not in stored
    assert tokens["access_token"][7:] not in stored


# -- PKCE -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"code_challenge_method": "plain"},
        {"code_challenge": ""},
        {"code_challenge": "short"},
    ],
)
async def test_authorize_without_s256_redirects_invalid_request(
    world: OAuthWorld, extra: dict[str, str]
) -> None:
    _, challenge = pkce_pair()
    async with world.http() as http:
        response = await world.authorize(http, challenge=challenge, extra=extra)
    assert response.status_code == 302
    back = query(response.headers["location"])
    assert response.headers["location"].startswith(REDIRECT)
    assert back["error"] == "invalid_request" and back["state"] == "st-123"


async def test_wrong_verifier_fails_and_burns_the_code(world: OAuthWorld) -> None:
    async with world.http() as http:
        verifier, challenge = pkce_pair()
        started = await world.authorize(http, challenge=challenge)
        code = query(
            (await world.consent(http, started.headers["location"])).json()["redirect_to"]
        )["code"]
        common = {"grant_type": "authorization_code", "client_id": CLIENT_ID, "code": code}
        wrong = await token_request(
            http, **common, code_verifier=pkce_pair()[0], redirect_uri=REDIRECT
        )
        right = await token_request(http, **common, code_verifier=verifier, redirect_uri=REDIRECT)
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    assert wrong.headers["cache-control"] == "no-store"
    assert right.status_code == 400 and right.json()["error"] == "invalid_grant"


async def test_code_is_single_use_and_a_replay_revokes_its_tokens(world: OAuthWorld) -> None:
    async with world.http() as http:
        verifier, challenge = pkce_pair()
        started = await world.authorize(http, challenge=challenge)
        code = query(
            (await world.consent(http, started.headers["location"])).json()["redirect_to"]
        )["code"]
        body = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
        }
        first = await token_request(http, **body)
        replay = await token_request(http, **body)
        after = await userinfo(http, first.json()["access_token"])
        refreshed = await token_request(
            http,
            grant_type="refresh_token",
            client_id=CLIENT_ID,
            refresh_token=first.json()["refresh_token"],
        )
    assert first.status_code == 200
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert after.status_code == 401
    assert refreshed.status_code == 400


async def test_expired_code_is_rejected(world: OAuthWorld) -> None:
    async with world.http() as http:
        verifier, challenge = pkce_pair()
        started = await world.authorize(http, challenge=challenge)
        code = query(
            (await world.consent(http, started.headers["location"])).json()["redirect_to"]
        )["code"]
        later = utc_now() + timedelta(seconds=61)
        world.stack.service.clock = lambda: later
        response = await token_request(
            http,
            grant_type="authorization_code",
            client_id=CLIENT_ID,
            code=code,
            code_verifier=verifier,
            redirect_uri=REDIRECT,
        )
    assert response.json()["error"] == "invalid_grant"


# -- redirect URI and client identity ------------------------------------------------------


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://evil.example/callback",
        REDIRECT + "/extra",
        REDIRECT + "?x=1",
        REDIRECT.upper(),
        "http://127.0.0.1:9999/cb",  # registered loopback with another port: exact match only
    ],
)
async def test_unregistered_redirect_uri_is_never_redirected_to(
    world: OAuthWorld, redirect_uri: str
) -> None:
    _, challenge = pkce_pair()
    async with world.http() as http:
        response = await world.authorize(http, challenge=challenge, redirect_uri=redirect_uri)
    assert response.status_code == 400
    assert "location" not in response.headers
    assert response.json()["error"] == "invalid_request"


async def test_token_request_must_repeat_the_exact_redirect_uri_and_client(
    world: OAuthWorld,
) -> None:
    async with world.http() as http:
        verifier, challenge = pkce_pair()
        for client_id, redirect_uri in (
            (CLIENT_ID, "http://127.0.0.1:8765/cb"),
            ("https://other.example/client.json", REDIRECT),
        ):
            started = await world.authorize(http, challenge=challenge)
            code = query(
                (await world.consent(http, started.headers["location"])).json()["redirect_to"]
            )["code"]
            response = await token_request(
                http,
                grant_type="authorization_code",
                client_id=client_id,
                code=code,
                code_verifier=verifier,
                redirect_uri=redirect_uri,
            )
            assert response.json()["error"] == "invalid_grant"


async def test_client_secret_basic_is_refused(world: OAuthWorld) -> None:
    async with world.http() as http:
        response = await http.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": "x"},
            headers={"Authorization": "Basic Zm9vOmJhcg=="},
        )
    assert response.status_code == 401 and response.json()["error"] == "invalid_client"


@pytest.mark.parametrize(
    ("client_id", "document"),
    [
        ("http://notes.example/oauth/client.json", None),
        ("https://notes.example/", None),
        ("https://notes.example/oauth/client.json#x", None),
        ("https://user@notes.example/oauth/client.json", None),
        (CLIENT_ID, client_document(client_id="https://notes.example/other.json")),
        (CLIENT_ID, client_document(client_secret="s3cret")),
        (CLIENT_ID, client_document(token_endpoint_auth_method="client_secret_basic")),
        (CLIENT_ID, client_document(redirect_uris=["javascript:alert(1)"])),
        (CLIENT_ID, client_document(redirect_uris=["http://notes.example/cb"])),
        (CLIENT_ID, client_document(redirect_uris=[])),
        (CLIENT_ID, b"not json"),
    ],
)
async def test_cimd_rejects_bad_client_ids_and_documents(client_id: str, document: Any) -> None:
    documents = Documents({CLIENT_ID: document} if document is not None else {})
    async with oauth_world(documents=documents) as world, world.http() as http:
        _, challenge = pkce_pair()
        response = await world.authorize(http, challenge=challenge, client_id=client_id)
    assert response.status_code == 400 and "location" not in response.headers
    assert response.json()["error"] in {"invalid_client", "invalid_request"}


async def test_cimd_on_a_private_address_is_blocked_before_any_request() -> None:
    documents = Documents({CLIENT_ID: client_document()})
    async with oauth_world(documents=documents) as world:

        async def private(host: str, port: int) -> list[str]:
            return ["10.0.0.7"]

        world.stack.service.clients._resolver = private
        async with world.http() as http:
            _, challenge = pkce_pair()
            response = await world.authorize(http, challenge=challenge)
    assert response.status_code == 400
    assert "client_id_host_blocked" in response.json()["error_description"]
    assert documents.fetches == []


async def test_cimd_document_is_cached(world: OAuthWorld) -> None:
    async with world.http() as http:
        for _ in range(3):
            _, challenge = pkce_pair()
            assert (await world.authorize(http, challenge=challenge)).status_code == 302
    assert world.documents.fetches == [CLIENT_ID]


# -- scopes -----------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["openid admin", "memory.read:secrets", "tools.use:Bad", ""])
async def test_unknown_scope_is_invalid_scope(world: OAuthWorld, scope: str) -> None:
    _, challenge = pkce_pair()
    async with world.http() as http:
        response = await world.authorize(http, challenge=challenge, scope=scope)
    assert response.status_code == 302
    assert query(response.headers["location"])["error"] == "invalid_scope"


async def test_completions_without_llm_generate_are_refused(world: OAuthWorld) -> None:
    async with world.http() as http:
        tokens = await world.login(http, scope="openid profile")
        response = await http.post(
            "/v1/chat/completions",
            json={"model": "mia:default", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    challenge = response.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge and 'scope="llm.generate"' in challenge
    assert world.gateway.requests == []


async def test_user_can_narrow_scopes_on_the_consent_screen(world: OAuthWorld) -> None:
    async with world.http() as http:
        tokens = await world.login(
            http,
            scope="openid email llm.generate memory.read:work",
            decision={"decision": "approve", "scopes": ["openid", "llm.generate"]},
        )
    assert tokens["scope"] == "openid llm.generate"


async def test_consent_choice_outside_the_request_is_rejected(world: OAuthWorld) -> None:
    _, challenge = pkce_pair()
    async with world.http() as http:
        started = await world.authorize(http, challenge=challenge, scope="openid")
        response = await world.consent(
            http,
            started.headers["location"],
            body={"decision": "approve", "scopes": ["openid", "llm.generate"]},
        )
    assert response.status_code == 422


async def test_refresh_cannot_widen_the_scope(world: OAuthWorld) -> None:
    async with world.http() as http:
        tokens = await world.login(http, scope="openid llm.generate")
        response = await token_request(
            http,
            grant_type="refresh_token",
            client_id=CLIENT_ID,
            refresh_token=tokens["refresh_token"],
            scope="openid llm.generate memory.read:work",
        )
        narrowed = await token_request(
            http,
            grant_type="refresh_token",
            client_id=CLIENT_ID,
            refresh_token=tokens["refresh_token"],
            scope="openid",
        )
    assert response.json()["error"] == "invalid_scope"
    assert narrowed.status_code == 200 and narrowed.json()["scope"] == "openid"


# -- consent screen -----------------------------------------------------------------------


async def test_consent_request_belongs_to_its_first_viewer(world: OAuthWorld) -> None:
    _, challenge = pkce_pair()
    async with world.http() as http:
        started = await world.authorize(http, challenge=challenge)
        handle = query(started.headers["location"])["request"]
        first = await http.get(
            f"/v1/oauth/requests/{handle}", headers={"x-test-user": str(world.user)}
        )
        other = await http.get(
            f"/v1/oauth/requests/{handle}", headers={"x-test-user": str(world.users[1])}
        )
        anonymous = await http.get(f"/v1/oauth/requests/{handle}")
        stolen = await http.post(
            f"/v1/oauth/requests/{handle}/decision",
            json={"decision": "approve"},
            headers={"x-test-user": str(world.users[1]), "Idempotency-Key": uuid.uuid4().hex},
        )
    assert first.status_code == 200
    view = first.json()
    assert view["client"]["name"] == "Notes" and view["client"]["redirect_host"] == "notes.example"
    assert [s["scope"] for s in view["scopes"]] == ["openid", "profile", "email", "llm.generate"]
    assert "health" not in view["memory_categories"]
    assert other.status_code == 404 and stolen.status_code == 404
    assert anonymous.status_code == 401


async def test_decision_replays_for_the_same_key_and_conflicts_for_another(
    world: OAuthWorld,
) -> None:
    _, challenge = pkce_pair()
    async with world.http() as http:
        started = await world.authorize(http, challenge=challenge)
        first = await world.consent(http, started.headers["location"], key="k" * 20)
        handle = query(started.headers["location"])["request"]
        headers = {"x-test-user": str(world.user)}
        again = await http.post(
            f"/v1/oauth/requests/{handle}/decision",
            json={"decision": "approve"},
            headers={**headers, "Idempotency-Key": "k" * 20},
        )
        other = await http.post(
            f"/v1/oauth/requests/{handle}/decision",
            json={"decision": "approve"},
            headers={**headers, "Idempotency-Key": "z" * 20},
        )
    assert again.json() == first.json()
    assert other.status_code == 409 and other.json()["error"]["code"] == "oauth_request_decided"


async def test_deny_redirects_access_denied(world: OAuthWorld) -> None:
    _, challenge = pkce_pair()
    async with world.http() as http:
        started = await world.authorize(http, challenge=challenge)
        decided = await world.consent(http, started.headers["location"], body={"decision": "deny"})
    back = query(decided.json()["redirect_to"])
    assert back == {"error": "access_denied", "state": "st-123", "iss": ISSUER}
    async with world.factory() as session:
        assert (await session.scalars(select(OAuthGrant))).all() == []


# -- refresh rotation and revocation --------------------------------------------------------


async def test_refresh_rotates_and_a_reused_refresh_token_kills_the_family(
    world: OAuthWorld,
) -> None:
    async with world.http() as http:
        tokens = await world.login(http)
        rotated = await token_request(
            http,
            grant_type="refresh_token",
            client_id=CLIENT_ID,
            refresh_token=tokens["refresh_token"],
        )
        assert rotated.status_code == 200
        new = rotated.json()
        assert new["refresh_token"] != tokens["refresh_token"]
        assert (await userinfo(http, new["access_token"])).status_code == 200
        reuse = await token_request(
            http,
            grant_type="refresh_token",
            client_id=CLIENT_ID,
            refresh_token=tokens["refresh_token"],
        )
        assert reuse.json()["error"] == "invalid_grant"
        assert (await userinfo(http, new["access_token"])).status_code == 401
        latest = await token_request(
            http,
            grant_type="refresh_token",
            client_id=CLIENT_ID,
            refresh_token=new["refresh_token"],
        )
    assert latest.json()["error"] == "invalid_grant"


async def test_revoking_a_refresh_token_revokes_its_access_tokens(world: OAuthWorld) -> None:
    async with world.http() as http:
        tokens = await world.login(http)
        foreign = await http.post(
            "/oauth/revoke",
            data={"token": tokens["refresh_token"], "client_id": "https://other.example/c.json"},
        )
        assert foreign.status_code == 200
        assert (await userinfo(http, tokens["access_token"])).status_code == 200
        revoked = await http.post(
            "/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": CLIENT_ID}
        )
        unknown = await http.post(
            "/oauth/revoke", data={"token": "garbage", "client_id": CLIENT_ID}
        )
        after = await userinfo(http, tokens["access_token"])
        refresh = await token_request(
            http,
            grant_type="refresh_token",
            client_id=CLIENT_ID,
            refresh_token=tokens["refresh_token"],
        )
    assert revoked.status_code == 200 and unknown.status_code == 200
    assert after.status_code == 401 and 'error="invalid_token"' in after.headers["www-authenticate"]
    assert refresh.json()["error"] == "invalid_grant"


async def test_revoking_an_access_token_keeps_the_refresh_token(world: OAuthWorld) -> None:
    async with world.http() as http:
        tokens = await world.login(http)
        await http.post(
            "/oauth/revoke", data={"token": tokens["access_token"], "client_id": CLIENT_ID}
        )
        assert (await userinfo(http, tokens["access_token"])).status_code == 401
        refresh = await token_request(
            http,
            grant_type="refresh_token",
            client_id=CLIENT_ID,
            refresh_token=tokens["refresh_token"],
        )
    assert refresh.status_code == 200


async def test_disconnecting_the_app_revokes_every_token_at_once(world: OAuthWorld) -> None:
    async with world.http() as http:
        tokens = await world.login(http)
        headers = {"x-test-user": str(world.user)}
        apps = (await http.get("/v1/apps", headers=headers)).json()["items"]
        assert [a["client_id"] for a in apps] == [CLIENT_ID]
        other = await http.delete(
            f"/v1/apps/{apps[0]['id']}",
            headers={"x-test-user": str(world.users[1]), "Idempotency-Key": uuid.uuid4().hex},
        )
        assert other.status_code == 404
        deleted = await http.delete(
            f"/v1/apps/{apps[0]['id']}", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}
        )
        again = await http.delete(
            f"/v1/apps/{apps[0]['id']}", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}
        )
        after = await userinfo(http, tokens["access_token"])
        refresh = await token_request(
            http,
            grant_type="refresh_token",
            client_id=CLIENT_ID,
            refresh_token=tokens["refresh_token"],
        )
        listed = (await http.get("/v1/apps", headers=headers)).json()["items"]
    assert deleted.status_code == 204 and again.status_code == 204
    assert after.status_code == 401 and refresh.json()["error"] == "invalid_grant"
    assert listed == []


async def test_expired_access_token_is_rejected(world: OAuthWorld) -> None:
    async with world.http() as http:
        tokens = await world.login(http)
        later = utc_now() + timedelta(seconds=901)
        world.stack.service.clock = lambda: later
        response = await userinfo(http, tokens["access_token"])
    assert response.status_code == 401


# -- resource indicators (RFC 8707) -------------------------------------------------------------


async def test_tokens_are_bound_to_their_resource(world: OAuthWorld) -> None:
    async with world.http() as http:
        mcp_token = await world.login(http, scope="llm.generate", resource=f"{ISSUER}/mcp")
        bad = await world.authorize(
            http, challenge=pkce_pair()[1], extra={"resource": "https://elsewhere.example/"}
        )
        response = await http.get(
            "/v1/models", headers={"Authorization": f"Bearer {mcp_token['access_token']}"}
        )
    assert query(bad.headers["location"])["error"] == "invalid_target"
    assert response.status_code == 401
