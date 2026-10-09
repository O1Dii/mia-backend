"""Test-side JWT/JWKS signing (RS256, ES256) used by the ID-token verification tests."""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64_json(obj: dict[str, Any]) -> str:
    return b64(json.dumps(obj, separators=(",", ":")).encode())


def _int_b64(value: int) -> str:
    return b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class RsaSigner:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict[str, Any]:
        numbers = self._key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "kid": self.kid,
            "alg": "RS256",
            "use": "sig",
            "n": _int_b64(numbers.n),
            "e": _int_b64(numbers.e),
        }

    def sign(self, claims: dict[str, Any], *, alg: str = "RS256", kid: str | None = None) -> str:
        header = {"alg": alg, "typ": "JWT", "kid": kid or self.kid}
        signing_input = f"{b64_json(header)}.{b64_json(claims)}"
        signature = self._key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{b64(signature)}"


class EcSigner:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self._key = ec.generate_private_key(ec.SECP256R1())

    def jwk(self) -> dict[str, Any]:
        numbers = self._key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "kid": self.kid,
            "alg": "ES256",
            "x": b64(numbers.x.to_bytes(32, "big")),
            "y": b64(numbers.y.to_bytes(32, "big")),
        }

    def sign(self, claims: dict[str, Any]) -> str:
        header = {"alg": "ES256", "typ": "JWT", "kid": self.kid}
        signing_input = f"{b64_json(header)}.{b64_json(claims)}"
        r, s = decode_dss_signature(
            self._key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        )
        return f"{signing_input}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def unsigned_token(claims: dict[str, Any], alg: str, kid: str) -> str:
    header = {"alg": alg, "typ": "JWT", "kid": kid}
    return f"{b64_json(header)}.{b64_json(claims)}."
