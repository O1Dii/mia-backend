from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.core.database import Database
from app.core.redis import RedisClient
from app.main import create_app
from app.modules.auth.composition import install_auth
from app.modules.auth.google import InvalidGoogleTokenError
from app.modules.auth.schemas import AuthNonceResponse, AuthSessionResponse, UserResponse
from app.modules.auth.service import AuthenticatedAccount, AuthService
from app.modules.auth.tokens import AccessPrincipal
from app.modules.users.models import User
from fastapi.testclient import TestClient

from tests.auth.fakes import fake_auth_cache


class StubAuthService:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.user = User(
            id=uuid4(),
            google_sub="subject",
            email="user@example.test",
            email_verified=True,
            display_name="User",
            avatar_url=None,
            locale="en",
            timezone="UTC",
            created_at=now,
            updated_at=now,
        )

    async def create_nonce(self) -> AuthNonceResponse:
        return AuthNonceResponse(
            nonce="n" * 32, expires_at=datetime.now(UTC) + timedelta(minutes=5)
        )

    async def exchange_google_token(self, **kwargs: object) -> AuthSessionResponse:
        del kwargs
        raise InvalidGoogleTokenError("bad token")

    async def authenticate_access(self, token: str) -> AuthenticatedAccount:
        assert token == "access-token"
        return AuthenticatedAccount(
            principal=AccessPrincipal(user_id=self.user.id, session_id=uuid4()),
            user=self.user,
        )

    @staticmethod
    def user_response(user: User) -> UserResponse:
        return AuthService.user_response(user)


def auth_client(*, nonce_limit: int = 10) -> TestClient:
    service = cast(AuthService, StubAuthService())
    cache = fake_auth_cache()
    settings = Settings(app_env="test", auth_nonce_rate_limit=nonce_limit)
    app = create_app(settings)
    install_auth(
        app,
        settings,
        database=Database(settings.database_url),
        redis=RedisClient(settings.redis_url),
        service=service,
        cache=cache,
    )
    return TestClient(app)


def test_nonce_endpoint_preserves_contract_and_request_id() -> None:
    with auth_client() as client:
        response = client.post(
            "/v1/auth/google/nonce",
            headers={"Idempotency-Key": "nonce-request-0001", "X-Request-ID": "auth-request"},
        )

    assert response.status_code == 201
    assert response.json()["nonce"] == "n" * 32
    assert response.headers["X-Request-ID"] == "auth-request"


def test_validation_error_does_not_echo_credentials() -> None:
    credential = "credential-that-must-not-be-returned"
    with auth_client() as client:
        response = client.post(
            "/v1/auth/google",
            headers={"Idempotency-Key": "exchange-request-02"},
            json={
                "id_token": credential,
                "nonce": "short",
                "device": {"platform": "android", "app_version": "1.0"},
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert credential not in response.text


def test_google_exchange_maps_verification_failure_without_detail_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with auth_client() as client:
        response = client.post(
            "/v1/auth/google",
            headers={"Idempotency-Key": "exchange-request-01"},
            json={
                "id_token": "not-logged-token",
                "nonce": "n" * 32,
                "device": {"platform": "android", "app_version": "1.0"},
            },
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert "bad token" not in response.text
    assert "not-logged-token" not in response.text
    assert "not-logged-token" not in caplog.text


def test_me_requires_bearer_and_rate_limit_has_retry_after() -> None:
    with auth_client(nonce_limit=1) as client:
        unauthorized = client.get("/v1/me")
        first = client.post(
            "/v1/auth/google/nonce", headers={"Idempotency-Key": "nonce-request-0001"}
        )
        limited = client.post(
            "/v1/auth/google/nonce", headers={"Idempotency-Key": "nonce-request-0002"}
        )
        me = client.get("/v1/me", headers={"Authorization": "Bearer access-token"})

    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "authentication_required"
    assert first.status_code == 201
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.test"
