from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.modules.models.chatgpt_plan.jwks import IdTokenError, JwksVerifier

from tests.models.jwt_tools import EcSigner, RsaSigner, b64_json, unsigned_token

ISSUER = "https://auth.openai.com"
CLIENT = "oaiapp_test"
NOW = 1_800_000_000.0


def claims(**overrides: Any) -> dict[str, Any]:
    base = {
        "iss": ISSUER,
        "aud": CLIENT,
        "nonce": "n-1",
        "exp": NOW + 3600,
        "iat": NOW - 5,
        "sub": "user",
    }
    base.update(overrides)
    return base


class JwksServer:
    def __init__(self, *signers: RsaSigner | EcSigner) -> None:
        self.keys = [s.jwk() for s in signers]
        self.fetches = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/.well-known/jwks.json"
            self.fetches += 1
            return httpx.Response(200, json={"keys": self.keys})

        return httpx.MockTransport(handler)


def verifier(server: JwksServer, clock: list[float] | None = None) -> JwksVerifier:
    now = clock if clock is not None else [NOW]
    return JwksVerifier(
        httpx.AsyncClient(transport=server.transport()), issuer=ISSUER, clock=lambda: now[0]
    )


async def test_valid_rs256_and_es256_tokens_verify() -> None:
    rsa_signer, ec_signer = RsaSigner("rsa-1"), EcSigner("ec-1")
    v = verifier(JwksServer(rsa_signer, ec_signer))
    assert (await v.verify(rsa_signer.sign(claims()), audience=CLIENT, nonce="n-1"))[
        "sub"
    ] == "user"
    assert (await v.verify(ec_signer.sign(claims()), audience=CLIENT, nonce="n-1"))["sub"] == "user"


async def test_audience_list_is_accepted() -> None:
    signer = RsaSigner("k")
    v = verifier(JwksServer(signer))
    token = signer.sign(claims(aud=["other", CLIENT]))
    assert await v.verify(token, audience=CLIENT)


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"iss": "https://evil.example"}, "issuer"),
        ({"aud": "someone-else"}, "audience"),
        ({"exp": NOW - 3600}, "expired"),
        ({"iat": NOW + 3600}, "future"),
        ({"nonce": "other"}, "nonce"),
    ],
)
async def test_claim_violations_are_rejected(overrides: dict[str, Any], why: str) -> None:
    signer = RsaSigner("k")
    v = verifier(JwksServer(signer))
    with pytest.raises(IdTokenError):
        await v.verify(signer.sign(claims(**overrides)), audience=CLIENT, nonce="n-1")


async def test_signature_from_another_key_is_rejected() -> None:
    trusted, attacker = RsaSigner("k"), RsaSigner("k")  # same kid, different key material
    v = verifier(JwksServer(trusted))
    with pytest.raises(IdTokenError, match="signature"):
        await v.verify(attacker.sign(claims()), audience=CLIENT, nonce="n-1")


async def test_tampered_payload_is_rejected() -> None:
    signer = RsaSigner("k")
    v = verifier(JwksServer(signer))
    head, _payload, sig = signer.sign(claims()).split(".")
    forged = f"{head}.{b64_json(claims(sub='admin'))}.{sig}"
    with pytest.raises(IdTokenError, match="signature"):
        await v.verify(forged, audience=CLIENT, nonce="n-1")


@pytest.mark.parametrize("alg", ["none", "HS256", "RS512"])
async def test_disallowed_algorithms_are_rejected_before_key_lookup(alg: str) -> None:
    server = JwksServer(RsaSigner("k"))
    v = verifier(server)
    with pytest.raises(IdTokenError, match="algorithm"):
        await v.verify(unsigned_token(claims(), alg, "k"), audience=CLIENT)
    assert server.fetches == 0


async def test_key_type_must_match_algorithm() -> None:
    ec_signer, rsa_signer = EcSigner("k"), RsaSigner("k")
    v = verifier(JwksServer(ec_signer))
    with pytest.raises(IdTokenError):
        await v.verify(rsa_signer.sign(claims()), audience=CLIENT)


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "!!.!!.!!"])
async def test_malformed_tokens_are_rejected(token: str) -> None:
    with pytest.raises(IdTokenError):
        await verifier(JwksServer(RsaSigner("k"))).verify(token, audience=CLIENT)


async def test_key_rotation_triggers_one_rate_limited_refetch() -> None:
    old, new = RsaSigner("old"), RsaSigner("new")
    server = JwksServer(old)
    clock = [NOW]
    v = verifier(server, clock)
    await v.verify(old.sign(claims()), audience=CLIENT)
    assert server.fetches == 1

    server.keys = [old.jwk(), new.jwk()]
    clock[0] += 120  # past the minimum refetch interval
    await v.verify(new.sign(claims(exp=clock[0] + 3600)), audience=CLIENT)
    assert server.fetches == 2

    unknown = RsaSigner("never-published")
    for _ in range(3):
        with pytest.raises(IdTokenError, match="key id"):
            await v.verify(unknown.sign(claims(exp=clock[0] + 3600)), audience=CLIENT)
    assert server.fetches == 2  # attackers cannot force a refetch storm


async def test_unreachable_jwks_fails_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    v = JwksVerifier(httpx.AsyncClient(transport=httpx.MockTransport(handler)), issuer=ISSUER)
    with pytest.raises(IdTokenError, match="signing keys"):
        await v.verify(RsaSigner("k").sign(claims(exp=9_999_999_999)), audience=CLIENT)
