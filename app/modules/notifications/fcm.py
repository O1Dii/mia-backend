"""Firebase Cloud Messaging HTTP v1 client (``projects/{id}/messages:send``).

Access tokens come from an :class:`AccessTokenProvider`; the production provider signs a
service-account JWT (``google-auth`` signer) and exchanges it at the token URI. The
service-account file is read only on first use and its contents are never logged.
Tests use a fake FCM endpoint and a static token provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_LIFETIME_S = 3600
TOKEN_REFRESH_MARGIN_S = 120

FcmOutcome = Literal["sent", "unregistered", "rejected", "retry", "auth_failed"]


class AccessTokenProvider(Protocol):
    async def token(self) -> str: ...

    def invalidate(self) -> None: ...


class StaticTokenProvider:
    """Fixed bearer token (tests, emulators)."""

    def __init__(self, value: str) -> None:
        self._value = value

    async def token(self) -> str:
        return self._value

    def invalidate(self) -> None:
        return None


class FcmCredentialsError(RuntimeError):
    """The service account could not be loaded or exchanged (details are not exposed)."""


class ServiceAccountTokenProvider:
    """OAuth 2.0 JWT-bearer grant for a Firebase service account."""

    def __init__(
        self,
        credentials_file: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._path = Path(credentials_file)
        self._http = http
        self._timeout_s = timeout_s
        self._cached: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._cached = None

    async def token(self) -> str:
        cached = self._cached
        if cached is not None and cached[1] > time.time():
            return cached[0]
        async with self._lock:
            cached = self._cached
            if cached is not None and cached[1] > time.time():
                return cached[0]
            value, lifetime = await self._exchange()
            self._cached = (value, time.time() + max(60, lifetime - TOKEN_REFRESH_MARGIN_S))
            return value

    async def _exchange(self) -> tuple[str, int]:
        from google.auth import crypt, jwt

        try:
            info = await asyncio.to_thread(self._load)
            signer = crypt.RSASigner.from_service_account_info(info)  # type: ignore[no-untyped-call]
            now = int(time.time())
            token_uri = str(info.get("token_uri") or "https://oauth2.googleapis.com/token")
            assertion = jwt.encode(  # type: ignore[no-untyped-call]
                signer,
                {
                    "iss": info["client_email"],
                    "scope": FCM_SCOPE,
                    "aud": token_uri,
                    "iat": now,
                    "exp": now + TOKEN_LIFETIME_S,
                },
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise FcmCredentialsError("service account is unusable") from error
        http = self._http or httpx.AsyncClient(timeout=self._timeout_s)
        try:
            response = await http.post(
                token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion.decode("ascii"),
                },
            )
        except httpx.HTTPError as error:
            raise FcmCredentialsError("token exchange failed") from error
        finally:
            if self._http is None:
                await http.aclose()
        if response.status_code != 200:
            raise FcmCredentialsError("token exchange was rejected")
        body = response.json()
        return str(body["access_token"]), int(body.get("expires_in", TOKEN_LIFETIME_S))

    def _load(self) -> dict[str, Any]:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("service account must be a JSON object")
        return data


@dataclass(frozen=True, slots=True)
class FcmResult:
    outcome: FcmOutcome
    message_name: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PushMessage:
    title: str
    body: str
    data: dict[str, str]
    collapse_key: str | None = None
    data_only: bool = False
    """No ``notification`` block: the app builds the notification itself (M70: consent
    request with «Разрешить»/«Отклонить» actions, silent wake-up for a device call)."""
    ttl_seconds: int = 86400


class FcmClient:
    def __init__(
        self,
        *,
        project_id: str,
        token_provider: AccessTokenProvider,
        endpoint: str = "https://fcm.googleapis.com",
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._url = f"{endpoint.rstrip('/')}/v1/projects/{project_id}/messages:send"
        self._tokens = token_provider
        self._http = http
        self._timeout_s = timeout_s

    def body(self, device_token: str, message: PushMessage) -> dict[str, Any]:
        android: dict[str, Any] = {"priority": "HIGH", "ttl": f"{message.ttl_seconds}s"}
        if message.collapse_key is not None:
            android["collapse_key"] = message.collapse_key
        body: dict[str, Any] = {"token": device_token, "data": message.data, "android": android}
        if not message.data_only:
            body["notification"] = {"title": message.title, "body": message.body}
        return {"message": body}

    async def send(self, device_token: str, message: PushMessage) -> FcmResult:
        try:
            access = await self._tokens.token()
        except FcmCredentialsError:
            logger.warning("fcm_credentials_unavailable")
            return FcmResult("auth_failed", error_code="push_auth_failed")
        http = self._http or httpx.AsyncClient(timeout=self._timeout_s)
        try:
            response = await http.post(
                self._url,
                json=self.body(device_token, message),
                headers={"Authorization": f"Bearer {access}"},
                timeout=self._timeout_s,
            )
        except httpx.HTTPError:
            return FcmResult("retry", error_code="push_unavailable")
        finally:
            if self._http is None:
                await http.aclose()
        return self._result(response)

    def _result(self, response: httpx.Response) -> FcmResult:
        status = response.status_code
        if status == 200:
            name = None
            try:
                name = str(response.json().get("name"))
            except (ValueError, AttributeError):
                name = None
            return FcmResult("sent", message_name=name)
        fcm_code = _fcm_error_code(response)
        if status == 404 or fcm_code == "UNREGISTERED":
            return FcmResult("unregistered", error_code="push_token_unregistered")
        if status in (401, 403):
            self._tokens.invalidate()
            return FcmResult("auth_failed", error_code="push_auth_failed")
        if status == 429 or status >= 500:
            return FcmResult("retry", error_code="push_unavailable")
        return FcmResult("rejected", error_code="push_rejected")


def _fcm_error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error", {})
        for detail in error.get("details", []) or []:
            code = detail.get("errorCode")
            if isinstance(code, str):
                return code
        status = error.get("status")
        return status if isinstance(status, str) else None
    except (ValueError, AttributeError):
        return None
