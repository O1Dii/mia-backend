"""PKCE (RFC 7636, S256) and single-use ``state`` helpers."""

from __future__ import annotations

import base64
import hashlib
import secrets


def new_verifier() -> str:
    """43..128 unreserved characters; 64 random bytes give 86."""
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def new_state() -> str:
    return secrets.token_urlsafe(32)


def hash_state(state: str) -> str:
    """Only the hash is stored: a database leak cannot complete someone's flow."""
    return hashlib.sha256(state.encode()).hexdigest()
