"""Minimal strict HS256 access JWTs and opaque refresh-token hashing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4


class InvalidAccessTokenError(ValueError):
    pass


class ExpiredAccessTokenError(InvalidAccessTokenError):
    pass


@dataclass(frozen=True, slots=True)
class AccessPrincipal:
    user_id: UUID
    session_id: UUID


class TokenManager:
    """Issue locally verifiable access JWTs without ever persisting bearer values."""

    def __init__(
        self,
        secret: str,
        *,
        access_ttl_seconds: int = 900,
        issuer: str = "mia-backend",
        audience: str = "mia-android",
    ) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("AUTH_JWT_SECRET must contain at least 32 bytes")
        self._secret = secret.encode()
        self._access_ttl_seconds = access_ttl_seconds
        self._issuer = issuer
        self._audience = audience

    @staticmethod
    def _encode_part(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    @staticmethod
    def _decode_part(value: str) -> dict[str, Any]:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        parsed = json.loads(decoded)
        if not isinstance(parsed, dict):
            raise ValueError("JWT part is not an object")
        return parsed

    def issue_access(
        self, user_id: UUID, session_id: UUID, *, now: datetime | None = None
    ) -> tuple[str, int]:
        issued_at = now or datetime.now(UTC)
        expiry = issued_at + timedelta(seconds=self._access_ttl_seconds)
        header = self._encode_part({"alg": "HS256", "typ": "JWT"})
        payload = self._encode_part(
            {
                "aud": self._audience,
                "exp": int(expiry.timestamp()),
                "iat": int(issued_at.timestamp()),
                "iss": self._issuer,
                "jti": str(uuid4()),
                "sid": str(session_id),
                "sub": str(user_id),
                "typ": "access",
            }
        )
        signing_input = f"{header}.{payload}"
        signature = hmac.new(self._secret, signing_input.encode(), hashlib.sha256).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        return f"{signing_input}.{encoded_signature}", self._access_ttl_seconds

    def verify_access(self, token: str, *, now: datetime | None = None) -> AccessPrincipal:
        try:
            header_part, payload_part, signature_part = token.split(".")
            header = self._decode_part(header_part)
            payload = self._decode_part(payload_part)
            supplied_signature = base64.b64decode(
                signature_part + "=" * (-len(signature_part) % 4),
                altchars=b"-_",
                validate=True,
            )
            expected_signature = hmac.new(
                self._secret, f"{header_part}.{payload_part}".encode(), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise InvalidAccessTokenError("access token is invalid")
            if header != {"alg": "HS256", "typ": "JWT"}:
                raise InvalidAccessTokenError("access token is invalid")
            if (
                payload.get("typ") != "access"
                or payload.get("iss") != self._issuer
                or payload.get("aud") != self._audience
            ):
                raise InvalidAccessTokenError("access token is invalid")
            current_timestamp = int((now or datetime.now(UTC)).timestamp())
            expires_at = payload.get("exp")
            issued_at = payload.get("iat")
            if not isinstance(expires_at, int) or expires_at <= current_timestamp:
                raise ExpiredAccessTokenError("access token has expired")
            if not isinstance(issued_at, int) or issued_at > current_timestamp + 30:
                raise InvalidAccessTokenError("access token is invalid")
            return AccessPrincipal(
                user_id=UUID(str(payload["sub"])), session_id=UUID(str(payload["sid"]))
            )
        except ExpiredAccessTokenError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, InvalidAccessTokenError):
                raise
            raise InvalidAccessTokenError("access token is invalid") from exc

    @staticmethod
    def new_refresh_token() -> str:
        return secrets.token_urlsafe(48)

    def hash_refresh_token(self, refresh_token: str) -> str:
        return hmac.new(self._secret, refresh_token.encode(), hashlib.sha256).hexdigest()

    def hash_web_session_token(self, session_token: str) -> str:
        """HMAC browser-session cookies in a separate domain from refresh tokens."""
        return hmac.new(
            self._secret, b"web-session:" + session_token.encode(), hashlib.sha256
        ).hexdigest()
