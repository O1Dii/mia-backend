"""The OAuth client against the test authorization server: discovery, PKCE, resource, SSRF."""

from __future__ import annotations

import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

import pytest
from app.modules.connections.oauth import pkce
from app.modules.connections.oauth.client import (
    InvalidGrantError,
    OAuthClient,
    Registration,
    ServerAuth,
    canonical_resource,
)
from app.modules.connections.oauth.errors import OAuthProtocolError, TransientOAuthError

from tests.catalog.remote_world import (
    AS_HOST,
    ISSUER,
    MCP_HOST,
    PRM_PATH,
    PUBLIC_IP,
    RESOURCE,
    AuthorizationServer,
    FakeClock,
    FakeResolver,
    RemoteWorld,
    browser_authorize,
    running,
    transport_factory,
)

REDIRECT = "https://mia.test.example/v1/connections/callback"


@dataclass
class Wired:
    world: RemoteWorld
    auth: AuthorizationServer
    client: OAuthClient
    resolver: FakeResolver
    clock: FakeClock


@asynccontextmanager
async def wired(*, require_auth: bool = True, **server: Any) -> AsyncIterator[Wired]:
    clock = FakeClock()
    auth = AuthorizationServer(clock=clock, **server)
    world = RemoteWorld(auth, require_auth=require_auth)
    resolver = FakeResolver()
    client = OAuthClient(resolver=resolver, http_transport=transport_factory(world), clock=clock)
    async with running(world):
        yield Wired(world, auth, client, resolver, clock)


async def discover(w: Wired, **kwargs: Any) -> ServerAuth:
    return await w.client.discover(RESOURCE, kwargs.get("network", [MCP_HOST]))


def test_pkce_challenge_is_s256_and_state_is_stored_hashed() -> None:
    # RFC 7636 appendix B
    assert (
        pkce.challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
    verifier = pkce.new_verifier()
    assert 43 <= len(verifier) <= 128 and verifier != pkce.new_verifier()
    state = pkce.new_state()
    assert pkce.hash_state(state) != state and len(pkce.hash_state(state)) == 64


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://MCP.Example.com/mcp/", "https://mcp.example.com/mcp"),
        ("https://mcp.example.com:443/mcp?x=1#f", "https://mcp.example.com/mcp"),
        ("https://mcp.example.com:8443/", "https://mcp.example.com:8443"),
        ("https://mcp.example.com", "https://mcp.example.com"),
    ],
)
def test_resource_identifier_is_canonical(url: str, expected: str) -> None:
    assert canonical_resource(url) == expected
    with pytest.raises(OAuthProtocolError):
        canonical_resource("http://mcp.example.com/mcp")


async def test_discovery_follows_the_challenge_to_the_authorization_server() -> None:
    async with wired() as w:
        server = await discover(w)
    assert server.resource == RESOURCE and server.issuer == ISSUER
    assert server.authorization_endpoint == f"{ISSUER}/authorize"
    assert server.token_endpoint == f"{ISSUER}/token"
    assert server.revocation_endpoint == f"{ISSUER}/revoke"
    assert server.registration_endpoint == f"{ISSUER}/register"
    assert server.scopes == "issues"  # from the challenge
    assert set(w.resolver.lookups) == {MCP_HOST, AS_HOST}


async def test_discovery_falls_back_to_the_well_known_document() -> None:
    async with wired(require_auth=False) as w:
        server = await discover(w)
    assert server.resource == RESOURCE and server.scopes == "issues"  # PRM scopes_supported


async def test_requires_auth_reads_the_probe_status() -> None:
    async with wired() as w:
        assert await w.client.requires_auth(RESOURCE, [MCP_HOST]) is True
    async with wired(require_auth=False) as open_world:
        assert await open_world.client.requires_auth(RESOURCE, [MCP_HOST]) is False


async def test_a_server_without_pkce_s256_is_unsupported() -> None:
    async with wired(supports_pkce=False) as w:
        with pytest.raises(OAuthProtocolError) as failure:
            await discover(w)
    assert failure.value.unsupported


@pytest.mark.parametrize(
    "override",
    [
        {"token_endpoint": "https://evil.example/token"},
        {"authorization_endpoint": "https://evil.example/authorize"},
        {"revocation_endpoint": "https://evil.example/revoke"},
        {"registration_endpoint": "https://evil.example/register"},
        {"token_endpoint": f"http://{AS_HOST}/token"},
        {"issuer": "https://other.example"},
        {"grant_types_supported": ["client_credentials"]},
    ],
)
async def test_endpoints_must_be_https_on_the_issuer_host(override: dict[str, Any]) -> None:
    async with wired(metadata_overrides=override) as w:
        with pytest.raises(OAuthProtocolError):
            await discover(w)


async def test_protected_resource_metadata_must_name_this_server() -> None:
    async with wired() as w:
        original = w.world._resource

        async def wrong_resource(scope: Any, receive: Any, send: Any, headers: Any) -> None:
            if scope["path"] == PRM_PATH:
                from starlette.responses import JSONResponse  # noqa: PLC0415

                body = {"resource": "https://other.example/mcp", "authorization_servers": [ISSUER]}
                await JSONResponse(body)(scope, receive, send)
                return
            await original(scope, receive, send, headers)

        w.world._resource = wrong_resource  # type: ignore[method-assign]
        with pytest.raises(OAuthProtocolError, match="another resource"):
            await discover(w)


@pytest.mark.parametrize("private", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"])
async def test_private_addresses_are_refused_during_discovery(private: str) -> None:
    async with wired() as w:
        w.resolver.mapping[AS_HOST] = [PUBLIC_IP]
        w.resolver.mapping[MCP_HOST] = [private]
        with pytest.raises(OAuthProtocolError, match="not allowed"):
            await discover(w)
        w.resolver.mapping[MCP_HOST] = [PUBLIC_IP]
        w.resolver.mapping[AS_HOST] = [PUBLIC_IP, private]  # one private answer poisons the set
        with pytest.raises(OAuthProtocolError, match="not allowed"):
            await discover(w)


async def test_a_challenge_may_not_point_at_another_host() -> None:
    async with wired() as w:
        original = w.world._resource

        async def redirecting(scope: Any, receive: Any, send: Any, headers: Any) -> None:
            if scope["path"] != PRM_PATH and not headers.get("authorization"):
                from starlette.responses import Response  # noqa: PLC0415

                challenge = 'Bearer resource_metadata="https://169.254.169.254/prm"'
                await Response(status_code=401, headers={"WWW-Authenticate": challenge})(
                    scope, receive, send
                )
                return
            await original(scope, receive, send, headers)

        w.world._resource = redirecting  # type: ignore[method-assign]
        with pytest.raises(OAuthProtocolError, match="not allowed"):
            await discover(w)


async def test_registration_prefers_cimd_then_registers_dynamically() -> None:
    async with wired() as w:
        server = await discover(w)
        dynamic = await w.client.register(server, redirect_uri=REDIRECT, cimd_url=None)
        assert dynamic.client_id.startswith("client-") and dynamic.auth_method == "none"
        assert w.auth.registrations[0]["redirect_uris"] == [REDIRECT]
        assert w.auth.registrations[0]["token_endpoint_auth_method"] == "none"
        # An authorization server that advertises CIMD gets Mia's metadata URL, no registration.
        cimd = replace(server, cimd_supported=True)
        url = "https://mia.test.example/v1/connections/client-metadata.json"
        chosen = await w.client.register(cimd, redirect_uri=REDIRECT, cimd_url=url)
        assert chosen == Registration(client_id=url, client_secret=None, auth_method="none")
        assert len(w.auth.registrations) == 1


async def test_no_registration_endpoint_and_no_cimd_is_unsupported() -> None:
    async with wired(supports_registration=False) as w:
        server = await discover(w)
        with pytest.raises(OAuthProtocolError) as failure:
            await w.client.register(server, redirect_uri=REDIRECT, cimd_url=None)
    assert failure.value.unsupported


async def test_authorization_code_flow_with_pkce_and_the_resource_indicator() -> None:
    async with wired() as w:
        server = await discover(w)
        registration = await w.client.register(server, redirect_uri=REDIRECT, cimd_url=None)
        state, verifier = pkce.new_state(), pkce.new_verifier()
        url = w.client.authorization_url(
            server.authorization_endpoint,
            client_id=registration.client_id,
            redirect_uri=REDIRECT,
            state=state,
            verifier=verifier,
            resource=server.resource,
            scope=server.scopes,
        )
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert query["code_challenge"] == pkce.challenge(verifier)
        assert query["code_challenge_method"] == "S256" and query["resource"] == RESOURCE
        assert query["state"] == state and query["scope"] == "issues"
        assert "code_verifier" not in query

        location = await browser_authorize(w.world, url)
        redirected = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        assert redirected["state"] == state

        tokens = await w.client.exchange_code(
            token_endpoint=server.token_endpoint,
            code=redirected["code"],
            verifier=verifier,
            redirect_uri=REDIRECT,
            resource=server.resource,
            client_id=registration.client_id,
            client_secret=None,
            auth_method="none",
        )
        assert tokens.access_token.startswith("at-") and tokens.refresh_token
        assert tokens.expires_at == w.clock.now + 3600 and tokens.scope == "issues"
        sent = w.auth.token_requests[0]
        assert sent["resource"] == RESOURCE and sent["code_verifier"] == verifier
        assert sent["client_id"] == registration.client_id
        # The code is single use and bound to the verifier.
        with pytest.raises(InvalidGrantError):
            await w.client.exchange_code(
                token_endpoint=server.token_endpoint,
                code=redirected["code"],
                verifier=verifier,
                redirect_uri=REDIRECT,
                resource=server.resource,
                client_id=registration.client_id,
                client_secret=None,
                auth_method="none",
            )


async def test_the_server_rejects_a_wrong_verifier_and_a_missing_resource() -> None:
    async with wired() as w:
        server = await discover(w)
        registration = await w.client.register(server, redirect_uri=REDIRECT, cimd_url=None)

        async def code(verifier: str) -> str:
            url = w.client.authorization_url(
                server.authorization_endpoint,
                client_id=registration.client_id,
                redirect_uri=REDIRECT,
                state="s",
                verifier=verifier,
                resource=server.resource,
                scope=None,
            )
            location = await browser_authorize(w.world, url)
            return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))["code"]

        with pytest.raises(InvalidGrantError):
            await w.client.exchange_code(
                token_endpoint=server.token_endpoint,
                code=await code(pkce.new_verifier()),
                verifier=pkce.new_verifier(),
                redirect_uri=REDIRECT,
                resource=server.resource,
                client_id=registration.client_id,
                client_secret=None,
                auth_method="none",
            )
        with pytest.raises(OAuthProtocolError):  # invalid_target is not an invalid grant
            verifier = pkce.new_verifier()
            await w.client.exchange_code(
                token_endpoint=server.token_endpoint,
                code=await code(verifier),
                verifier=verifier,
                redirect_uri=REDIRECT,
                resource="https://other.example/mcp",
                client_id=registration.client_id,
                client_secret=None,
                auth_method="none",
            )


async def connect(w: Wired) -> tuple[ServerAuth, Registration, Any]:
    server = await discover(w)
    registration = await w.client.register(server, redirect_uri=REDIRECT, cimd_url=None)
    verifier = pkce.new_verifier()
    url = w.client.authorization_url(
        server.authorization_endpoint,
        client_id=registration.client_id,
        redirect_uri=REDIRECT,
        state="s",
        verifier=verifier,
        resource=server.resource,
        scope=None,
    )
    location = await browser_authorize(w.world, url)
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))["code"]
    tokens = await w.client.exchange_code(
        token_endpoint=server.token_endpoint,
        code=code,
        verifier=verifier,
        redirect_uri=REDIRECT,
        resource=server.resource,
        client_id=registration.client_id,
        client_secret=None,
        auth_method="none",
    )
    return server, registration, tokens


async def test_refresh_rotates_and_a_rejected_refresh_is_an_invalid_grant() -> None:
    async with wired() as w:
        server, registration, tokens = await connect(w)
        assert tokens.refresh_token is not None
        fresh = await w.client.refresh(
            token_endpoint=server.token_endpoint,
            refresh_token=tokens.refresh_token,
            resource=server.resource,
            scope=None,
            client_id=registration.client_id,
            client_secret=None,
            auth_method="none",
        )
        assert fresh.access_token != tokens.access_token
        assert fresh.refresh_token and fresh.refresh_token != tokens.refresh_token
        assert w.auth.token_requests[-1]["resource"] == RESOURCE
        with pytest.raises(InvalidGrantError):  # the old refresh token was rotated away
            await w.client.refresh(
                token_endpoint=server.token_endpoint,
                refresh_token=tokens.refresh_token,
                resource=server.resource,
                scope=None,
                client_id=registration.client_id,
                client_secret=None,
                auth_method="none",
            )


async def test_server_errors_are_transient_and_odd_token_types_are_refused() -> None:
    async with wired() as w:
        server, registration, tokens = await connect(w)
        assert tokens.refresh_token is not None
        original = w.auth._token

        async def broken(request: Any) -> Any:
            from starlette.responses import Response  # noqa: PLC0415

            return Response(status_code=503)

        w.auth._token = broken  # type: ignore[method-assign]
        with pytest.raises(TransientOAuthError):
            await w.client.refresh(
                token_endpoint=server.token_endpoint,
                refresh_token=tokens.refresh_token,
                resource=server.resource,
                scope=None,
                client_id=registration.client_id,
                client_secret=None,
                auth_method="none",
            )

        async def mac(request: Any) -> Any:
            from starlette.responses import JSONResponse  # noqa: PLC0415

            return JSONResponse({"access_token": "x", "token_type": "mac"})

        w.auth._token = mac  # type: ignore[method-assign]
        with pytest.raises(OAuthProtocolError, match="token_type"):
            await w.client.refresh(
                token_endpoint=server.token_endpoint,
                refresh_token=tokens.refresh_token,
                resource=server.resource,
                scope=None,
                client_id=registration.client_id,
                client_secret=None,
                auth_method="none",
            )
        w.auth._token = original  # type: ignore[method-assign]


async def test_revocation_sends_both_tokens_and_the_server_invalidates_them() -> None:
    async with wired() as w:
        server, registration, tokens = await connect(w)
        assert tokens.refresh_token is not None
        assert server.revocation_endpoint is not None
        accepted = await w.client.revoke(
            revocation_endpoint=server.revocation_endpoint,
            tokens=[(tokens.refresh_token, "refresh_token"), (tokens.access_token, "access_token")],
            client_id=registration.client_id,
            client_secret=None,
            auth_method="none",
        )
        assert accepted is True
        assert {(r["token"], r["token_type_hint"]) for r in w.auth.revocations} == {
            (tokens.refresh_token, "refresh_token"),
            (tokens.access_token, "access_token"),
        }
        assert w.auth.live_tokens() == []
        assert not w.auth.access_valid(tokens.access_token, RESOURCE)


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
async def test_confidential_clients_authenticate_at_the_token_endpoint(method: str) -> None:
    async with wired(
        confidential=method, metadata_overrides={"token_endpoint_auth_methods_supported": [method]}
    ) as w:
        server = await discover(w)
        registration = await w.client.register(server, redirect_uri=REDIRECT, cimd_url=None)
        assert registration.auth_method == method and registration.client_secret
        verifier = pkce.new_verifier()
        url = w.client.authorization_url(
            server.authorization_endpoint,
            client_id=registration.client_id,
            redirect_uri=REDIRECT,
            state="s",
            verifier=verifier,
            resource=server.resource,
            scope=None,
        )
        location = await browser_authorize(w.world, url)
        code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))["code"]

        async def exchange(secret: str | None) -> Any:
            return await w.client.exchange_code(
                token_endpoint=server.token_endpoint,
                code=code,
                verifier=verifier,
                redirect_uri=REDIRECT,
                resource=server.resource,
                client_id=registration.client_id,
                client_secret=secret,
                auth_method=method,
            )

        with pytest.raises(OAuthProtocolError):  # wrong secret: invalid_client
            await exchange("wrong")
        # the code survives a failed client authentication only if the server keeps it; ours
        # consumes it on use, so a fresh authorization is needed for the right secret.
        location = await browser_authorize(w.world, url)
        code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))["code"]
        tokens = await exchange(registration.client_secret)
        assert tokens.access_token
        sent = w.auth.token_requests[-1]
        assert ("client_secret" in sent) == (method == "client_secret_post")
