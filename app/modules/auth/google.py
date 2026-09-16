"""Google ID-token verification behind a testable provider boundary."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from google.auth import exceptions as google_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token


class InvalidGoogleTokenError(ValueError):
    """Raised without exposing provider verification details to callers."""


class GoogleVerificationUnavailableError(RuntimeError):
    """Google signing certificates could not be fetched."""


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    subject: str
    email: str
    email_verified: bool
    display_name: str | None
    avatar_url: str | None
    locale: str


class GoogleVerifier(Protocol):
    async def verify(self, id_token: str, *, audience: str, nonce: str) -> GoogleIdentity: ...


class GoogleIdTokenVerifier:
    """Verify Google signatures and every security-sensitive identity claim."""

    def __init__(
        self,
        *,
        verify_token: Callable[[str, Any, str], Mapping[str, Any]] | None = None,
    ) -> None:
        self._verify_token = verify_token or google_id_token.verify_oauth2_token

    async def verify(self, id_token: str, *, audience: str, nonce: str) -> GoogleIdentity:
        try:
            claims = await asyncio.to_thread(self._verify_sync, id_token, audience)
            return self._validated_identity(claims, audience=audience, nonce=nonce)
        except google_exceptions.TransportError as exc:
            raise GoogleVerificationUnavailableError("Google verification is unavailable") from exc
        except (google_exceptions.GoogleAuthError, KeyError, TypeError, ValueError) as exc:
            raise InvalidGoogleTokenError("Google ID token is invalid") from exc

    def _verify_sync(self, token: str, audience: str) -> Mapping[str, Any]:
        request = google_requests.Request()
        try:
            # google-auth calls the transport positionally (url, method=...); accept both
            # forms and cap each certificate fetch.
            def bounded_request(*args: Any, **kwargs: Any) -> Any:
                return request(*args, **{**kwargs, "timeout": 10})

            return self._verify_token(token, bounded_request, audience)
        finally:
            request.session.close()

    @staticmethod
    def _validated_identity(
        claims: Mapping[str, Any], *, audience: str, nonce: str
    ) -> GoogleIdentity:
        token_audience = claims["aud"]
        audience_matches = token_audience == audience or (
            isinstance(token_audience, list) and audience in token_audience
        )
        if not audience_matches:
            raise InvalidGoogleTokenError("Google ID token is invalid")
        if claims["iss"] not in {"accounts.google.com", "https://accounts.google.com"}:
            raise InvalidGoogleTokenError("Google ID token is invalid")
        expiry = claims["exp"]
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, int | float)
            or not math.isfinite(expiry)
            or expiry <= datetime.now(UTC).timestamp()
        ):
            raise InvalidGoogleTokenError("Google ID token is invalid")
        if claims.get("nonce") != nonce:
            raise InvalidGoogleTokenError("Google ID token is invalid")

        subject = claims["sub"]
        email = claims["email"]
        verified = claims.get("email_verified", False)
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise InvalidGoogleTokenError("Google ID token is invalid")
        if not isinstance(email, str) or not email or len(email) > 320:
            raise InvalidGoogleTokenError("Google ID token is invalid")
        if not isinstance(verified, bool):
            raise InvalidGoogleTokenError("Google ID token is invalid")

        def optional_string(name: str, maximum: int) -> str | None:
            value = claims.get(name)
            if value is None:
                return None
            if not isinstance(value, str) or len(value) > maximum:
                raise InvalidGoogleTokenError("Google ID token is invalid")
            return value

        locale = optional_string("locale", 35) or "en"
        if len(locale) < 2:
            locale = "en"
        return GoogleIdentity(
            subject=subject,
            email=email,
            email_verified=verified,
            display_name=optional_string("name", 160),
            avatar_url=optional_string("picture", 2048),
            locale=locale,
        )
