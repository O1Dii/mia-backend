"""OpenID Connect ID tokens (ES256) and the JWKS that verifies them.

The P-256 signing key is derived from ``AUTH_JWT_SECRET`` with HKDF (own ``info``
label), so it is stable across restarts and replicas without a new secret; rotating
``AUTH_JWT_SECRET`` rotates it. Only the public part is ever published.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any, Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO: Final = b"mia-oauth-id-token-es256-v1"
_P256_ORDER: Final = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class IdTokenSigner:
    def __init__(self, secret: bytes) -> None:
        material = HKDF(algorithm=hashes.SHA256(), length=48, salt=None, info=_INFO).derive(secret)
        scalar = int.from_bytes(material, "big") % (_P256_ORDER - 1) + 1
        self._key = ec.derive_private_key(scalar, ec.SECP256R1())
        numbers = self._key.public_key().public_numbers()
        self._x = _b64(numbers.x.to_bytes(32, "big"))
        self._y = _b64(numbers.y.to_bytes(32, "big"))
        thumb = json.dumps(
            {"crv": "P-256", "kty": "EC", "x": self._x, "y": self._y},
            separators=(",", ":"),
            sort_keys=True,
        )
        self.kid = _b64(hashlib.sha256(thumb.encode()).digest())[:16]

    def jwks(self) -> dict[str, Any]:
        return {
            "keys": [
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "x": self._x,
                    "y": self._y,
                    "use": "sig",
                    "alg": "ES256",
                    "kid": self.kid,
                }
            ]
        }

    def sign(self, claims: Mapping[str, Any]) -> str:
        header = {"alg": "ES256", "typ": "JWT", "kid": self.kid}
        signing_input = (
            _b64(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64(json.dumps(dict(claims), separators=(",", ":"), ensure_ascii=False).encode())
        )
        der = self._key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return signing_input + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def id_token(
        self,
        *,
        issuer: str,
        subject: str,
        audience: str,
        auth_time: int,
        nonce: str | None,
        ttl_s: int,
        extra: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> str:
        issued = int(now if now is not None else time.time())
        claims: dict[str, Any] = {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "iat": issued,
            "exp": issued + ttl_s,
            "auth_time": auth_time,
        }
        if nonce:
            claims["nonce"] = nonce
        if extra:
            claims.update(extra)
        return self.sign(claims)
