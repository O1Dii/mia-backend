from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.modules.auth.google import GoogleIdTokenVerifier, InvalidGoogleTokenError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt, jwt
from google.oauth2 import id_token as google_id_token


def valid_claims() -> dict[str, Any]:
    return {
        "aud": "web-client-id",
        "iss": "https://accounts.google.com",
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "iat": int(datetime.now(UTC).timestamp()),
        "nonce": "n" * 32,
        "sub": "google-subject",
        "email": "person@example.test",
        "email_verified": True,
        "name": "Mia User",
        "picture": "https://example.test/avatar.png",
        "locale": "ru",
    }


@pytest.mark.asyncio
async def test_verifier_passes_expected_audience_to_google_library() -> None:
    seen: list[tuple[str, str]] = []

    def verify(token: str, request: object, audience: str) -> dict[str, Any]:
        del request
        seen.append((token, audience))
        return valid_claims()

    identity = await GoogleIdTokenVerifier(verify_token=verify).verify(
        "signed-token", audience="web-client-id", nonce="n" * 32
    )

    assert seen == [("signed-token", "web-client-id")]
    assert identity.subject == "google-subject"
    assert identity.email == "person@example.test"


@pytest.mark.asyncio
async def test_verifier_rejects_signature_failure() -> None:
    def verify(token: str, request: object, audience: str) -> dict[str, Any]:
        del token, request, audience
        raise ValueError("signature mismatch")

    with pytest.raises(InvalidGoogleTokenError, match="invalid"):
        await GoogleIdTokenVerifier(verify_token=verify).verify(
            "bad-signature", audience="web-client-id", nonce="n" * 32
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("aud", "another-client"),
        ("iss", "https://attacker.example"),
        ("exp", 0),
        ("exp", float("nan")),
        ("exp", True),
        ("nonce", "x" * 32),
        ("sub", ""),
        ("nonce", None),
        ("aud", []),
    ],
)
async def test_verifier_rejects_invalid_security_claims(claim: str, value: Any) -> None:
    claims = valid_claims()
    claims[claim] = value

    def verify(token: str, request: object, audience: str) -> dict[str, Any]:
        del token, request, audience
        return claims

    with pytest.raises(InvalidGoogleTokenError):
        await GoogleIdTokenVerifier(verify_token=verify).verify(
            "signed-token", audience="web-client-id", nonce="n" * 32
        )


@pytest.mark.asyncio
async def test_real_google_library_verifies_rsa_signature_and_rejects_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    monkeypatch.setattr(google_id_token, "_fetch_certs", lambda *a: {"test-key": public_pem})
    signer = crypt.RSASigner.from_string(private_pem, key_id="test-key")  # type: ignore[no-untyped-call]
    token = jwt.encode(signer, valid_claims()).decode()  # type: ignore[no-untyped-call]
    verifier = GoogleIdTokenVerifier()
    identity = await verifier.verify(token, audience="web-client-id", nonce="n" * 32)
    assert identity.subject == "google-subject"
    header, payload, signature = token.split(".")
    changed_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(InvalidGoogleTokenError):
        await verifier.verify(
            f"{header}.{payload}.{changed_signature}", audience="web-client-id", nonce="n" * 32
        )
