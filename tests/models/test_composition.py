from __future__ import annotations

import pytest
from app.core.config import Settings
from app.core.crypto import generate_master_key
from app.core.ports import ModelGateway
from app.main import create_app
from app.modules.models.composition import build_keyring
from fastapi.testclient import TestClient
from pydantic import ValidationError


def test_app_mounts_the_connections_api_and_exposes_the_gateway() -> None:
    app = create_app(Settings(app_env="test"))
    assert isinstance(app.state.model_gateway, ModelGateway)
    paths = set(app.openapi()["paths"])
    assert {"/v1/model-connections", "/v1/model-connections/{connection_id}/test"} <= paths
    with TestClient(app) as client:  # lifespan start/stop closes the gateway HTTP client
        unauthenticated = client.get("/v1/model-connections")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["code"] == "unauthenticated"
    assert app.state.models_stack.client.is_closed


def test_production_requires_master_keys_and_they_stay_out_of_repr() -> None:
    base = {
        "app_env": "production",
        "google_client_id": "client",
        "auth_jwt_secret": "x" * 40,
    }
    with pytest.raises(ValidationError, match="CREDENTIALS_MASTER_KEYS"):
        Settings(**base)  # type: ignore[arg-type]
    key = generate_master_key()
    settings = Settings(**base, credentials_master_keys=f"k1:{key}")  # type: ignore[arg-type]
    assert key not in repr(settings)
    assert build_keyring(settings).active_id == "k1"


def test_local_development_gets_a_fixed_dev_keyring() -> None:
    assert build_keyring(Settings(app_env="test")).active_id == "dev-local"
    two = Settings(
        app_env="test",
        credentials_master_keys=f"a:{generate_master_key()},b:{generate_master_key()}",
        credentials_master_key_active="a",
    )
    assert build_keyring(two).active_id == "a"
