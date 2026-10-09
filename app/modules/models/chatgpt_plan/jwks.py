"""ID-token verification against the issuer's JWKS (RS256/ES256 only).

M01 decoded the ID token without checking its signature; M11 must verify it
(DECISION.md, «Подпись ID-токена»). Only asymmetric algorithms from an allow-list
are accepted: ``alg=none`` and HMAC confusion are rejected before any key lookup.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from collections.abc import Callable
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

ALLOWED_ALGS = frozenset({"RS256", "ES256"})
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
CLOCK_LEEWAY_SECONDS = 60
JWKS_TTL_SECONDS = 3600
JWKS_MIN_REFETCH_SECONDS = 60


class IdTokenError(Exception):
    """The ID token failed verification; the message is content-free."""


def _b64(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except ValueError:
        raise IdTokenError("ID token is not valid base64url.") from None


def _int(value: str) -> int:
    return int.from_bytes(_b64(value), "big")


class JwksVerifier:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        issuer: str,
        jwks_url: str = JWKS_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._issuer = issuer
        self._url = jwks_url
        self._clock = clock
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self, *, force: bool) -> None:
        async with self._lock:
            age = self._clock() - self._fetched_at
            if self._keys and age < (JWKS_MIN_REFETCH_SECONDS if force else JWKS_TTL_SECONDS):
                return
            try:
                response = await self._client.get(self._url)
                payload = response.json() if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                payload = None
            keys = payload.get("keys") if isinstance(payload, dict) else None
            if not isinstance(keys, list):
                raise IdTokenError("The signing keys could not be loaded.")
            self._keys = {
                str(k["kid"]): k for k in keys if isinstance(k, dict) and k.get("kid") is not None
            }
            self._fetched_at = self._clock()

    async def _key_for(self, kid: str) -> dict[str, Any]:
        await self._refresh(force=False)
        if kid not in self._keys:
            await self._refresh(force=True)  # key rotation: one rate-limited refetch
        try:
            return self._keys[kid]
        except KeyError:
            raise IdTokenError("The ID token key id is unknown.") from None

    async def verify(
        self, token: str, *, audience: str, nonce: str | None = None
    ) -> dict[str, Any]:
        """Verify signature, ``iss``, ``aud``, ``exp``, ``iat`` and (optionally) ``nonce``."""
        parts = token.split(".")
        if len(parts) != 3:
            raise IdTokenError("ID token is malformed.")
        try:
            header = json.loads(_b64(parts[0]))
            claims = json.loads(_b64(parts[1]))
        except ValueError:
            raise IdTokenError("ID token is malformed.") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise IdTokenError("ID token is malformed.")
        alg = header.get("alg")
        if alg not in ALLOWED_ALGS or not isinstance(header.get("kid"), str):
            raise IdTokenError("ID token uses an unsupported algorithm.")
        jwk = await self._key_for(header["kid"])
        if jwk.get("alg") not in (None, alg):
            raise IdTokenError("ID token algorithm does not match its key.")
        self._check_signature(
            jwk, alg, f"{parts[0]}.{parts[1]}".encode("ascii", "ignore"), _b64(parts[2])
        )
        self._check_claims(claims, audience, nonce)
        return claims

    @staticmethod
    def _check_signature(jwk: dict[str, Any], alg: str, signed: bytes, signature: bytes) -> None:
        try:
            if alg == "RS256" and jwk.get("kty") == "RSA":
                rsa.RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key().verify(
                    signature, signed, padding.PKCS1v15(), hashes.SHA256()
                )
            elif alg == "ES256" and jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
                if len(signature) != 64:
                    raise IdTokenError("ID token signature is invalid.")
                public = ec.EllipticCurvePublicNumbers(
                    _int(jwk["x"]), _int(jwk["y"]), ec.SECP256R1()
                ).public_key()
                public.verify(
                    encode_dss_signature(
                        int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
                    ),
                    signed,
                    ec.ECDSA(hashes.SHA256()),
                )
            else:
                raise IdTokenError("ID token key type does not match its algorithm.")
        except (InvalidSignature, KeyError, ValueError):
            raise IdTokenError("ID token signature is invalid.") from None

    def _check_claims(self, claims: dict[str, Any], audience: str, nonce: str | None) -> None:
        now = self._clock()
        if claims.get("iss") != self._issuer:
            raise IdTokenError("ID token issuer mismatch.")
        aud = claims.get("aud")
        if not (aud == audience or (isinstance(aud, list) and audience in aud)):
            raise IdTokenError("ID token audience mismatch.")
        exp = claims.get("exp")
        if not isinstance(exp, int | float) or exp + CLOCK_LEEWAY_SECONDS < now:
            raise IdTokenError("ID token has expired.")
        iat = claims.get("iat")
        if isinstance(iat, int | float) and iat - CLOCK_LEEWAY_SECONDS > now:
            raise IdTokenError("ID token was issued in the future.")
        if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise IdTokenError("ID token nonce mismatch.")
