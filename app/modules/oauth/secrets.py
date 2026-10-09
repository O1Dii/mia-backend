"""Credential primitives: opaque tokens, hashing at rest, PKCE S256, constant-time checks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from typing import Final, Literal

TokenKind = Literal["access", "refresh", "code", "request"]
PREFIXES: Final[dict[TokenKind, str]] = {
    "access": "mia_at_",
    "refresh": "mia_rt_",
    "code": "mia_ac_",
    "request": "mia_rq_",
}
_BODY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def new_secret(kind: TokenKind) -> str:
    """``<prefix>`` + 32 random bytes (base64url, 43 characters)."""
    return PREFIXES[kind] + secrets.token_urlsafe(32)


def looks_like(value: str, kind: TokenKind) -> bool:
    prefix = PREFIXES[kind]
    return value.startswith(prefix) and bool(_BODY.match(value[len(prefix) :]))


def digest(value: str) -> str:
    """SHA-256 hex digest stored instead of the credential (256-bit random input)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def same(left: str, right: str) -> bool:
    """Constant-time string comparison."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def valid_challenge(challenge: str) -> bool:
    """A S256 challenge is base64url(SHA-256) without padding: exactly 43 characters."""
    return bool(_CHALLENGE.match(challenge))


def verify_pkce(verifier: str, challenge: str) -> bool:
    """RFC 7636 S256 only; ``plain`` is never accepted."""
    if not _VERIFIER.match(verifier):
        return False
    computed = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return same(computed, challenge)
