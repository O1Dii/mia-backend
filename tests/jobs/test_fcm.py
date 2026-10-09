"""FCM HTTP v1 client against a fake endpoint; service-account token exchange offline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.modules.notifications.fcm import (
    FcmClient,
    PushMessage,
    ServiceAccountTokenProvider,
    StaticTokenProvider,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import jwt

from tests.jobs.conftest import FCM_ENDPOINT, FCM_PROJECT, FakeFcm

MESSAGE = PushMessage(
    title="Готово: сводка",
    body="Сводка готова.",
    data={"type": "inbox_item", "inbox_item_id": "x"},
    collapse_key="x",
)


async def test_send_posts_http_v1_message() -> None:
    fcm = FakeFcm()
    result = await fcm.client().send("device-token-1", MESSAGE)
    assert result.outcome == "sent"
    assert result.message_name and result.message_name.startswith(f"projects/{FCM_PROJECT}/")
    assert fcm.auth == ["Bearer test-access-token"]
    assert fcm.sent == [
        {
            "token": "device-token-1",
            "notification": {"title": "Готово: сводка", "body": "Сводка готова."},
            "data": {"type": "inbox_item", "inbox_item_id": "x"},
            "android": {"priority": "HIGH", "ttl": "86400s", "collapse_key": "x"},
        }
    ]


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (404, {"error": {"status": "NOT_FOUND"}}, "unregistered"),
        (
            400,
            {
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "details": [{"errorCode": "UNREGISTERED"}],
                }
            },
            "unregistered",
        ),
        (400, {"error": {"status": "INVALID_ARGUMENT"}}, "rejected"),
        (429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, "retry"),
        (503, {}, "retry"),
        (401, {}, "auth_failed"),
    ],
)
async def test_error_mapping(status: int, body: dict[str, Any], outcome: str) -> None:
    fcm = FakeFcm(responses={"t": (status, body)})
    result = await fcm.client().send("t", MESSAGE)
    assert result.outcome == outcome


async def test_transport_error_is_retryable() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = FcmClient(
        project_id=FCM_PROJECT,
        token_provider=StaticTokenProvider("a"),
        endpoint=FCM_ENDPOINT,
        http=httpx.AsyncClient(transport=httpx.MockTransport(broken)),
    )
    assert (await client.send("t", MESSAGE)).outcome == "retry"


async def test_service_account_token_exchange_and_cache(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    path = tmp_path / "sa.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "push@mia-test.iam.gserviceaccount.com",
                "private_key": pem,
                "private_key_id": "k1",
                "token_uri": "https://oauth.test/token",
            }
        )
    )
    exchanges: list[dict[str, str]] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        exchanges.append(form)
        return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})

    provider = ServiceAccountTokenProvider(
        str(path), http=httpx.AsyncClient(transport=httpx.MockTransport(token_endpoint))
    )
    assert await provider.token() == "ya29.test"
    assert await provider.token() == "ya29.test"
    assert len(exchanges) == 1
    assert exchanges[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    claims = jwt.decode(  # type: ignore[no-untyped-call]
        exchanges[0]["assertion"], certs=public_pem, audience="https://oauth.test/token"
    )
    assert claims["iss"] == "push@mia-test.iam.gserviceaccount.com"
    assert claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
    provider.invalidate()
    await provider.token()
    assert len(exchanges) == 2


async def test_unusable_service_account_is_auth_failed(tmp_path: Path) -> None:
    missing = ServiceAccountTokenProvider(str(tmp_path / "absent.json"))
    client = FcmClient(project_id=FCM_PROJECT, token_provider=missing, endpoint=FCM_ENDPOINT)
    result = await client.send("t", MESSAGE)
    assert (result.outcome, result.error_code) == ("auth_failed", "push_auth_failed")
