from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.modules.auth.tokens import (
    ExpiredAccessTokenError,
    InvalidAccessTokenError,
    TokenManager,
)

SECRET = "test-only-auth-signing-secret-at-least-32-bytes"


def test_access_token_round_trip_and_signature_rejection() -> None:
    manager = TokenManager(SECRET)
    user_id = uuid4()
    family_id = uuid4()
    token, expires_in = manager.issue_access(user_id, family_id)

    assert expires_in == 900
    assert manager.verify_access(token).user_id == user_id
    header, payload, signature = token.split(".")
    signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    tampered = f"{header}.{payload}.{signature}"
    with pytest.raises(InvalidAccessTokenError):
        manager.verify_access(tampered)


def test_access_token_expiry_is_distinct() -> None:
    manager = TokenManager(SECRET, access_ttl_seconds=60)
    issued_at = datetime(2026, 1, 1, tzinfo=UTC)
    token, _ = manager.issue_access(uuid4(), uuid4(), now=issued_at)

    with pytest.raises(ExpiredAccessTokenError):
        manager.verify_access(token, now=issued_at + timedelta(seconds=60))


def test_refresh_tokens_are_opaque_and_only_stable_when_hashed() -> None:
    manager = TokenManager(SECRET)
    first = manager.new_refresh_token()
    second = manager.new_refresh_token()

    assert len(first) >= 32
    assert first != second
    assert manager.hash_refresh_token(first) == manager.hash_refresh_token(first)
    assert first not in manager.hash_refresh_token(first)
