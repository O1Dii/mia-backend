"""Configuration tests."""

import pytest
from app.core.config import Settings
from pydantic import ValidationError


def test_settings_default_to_fake_provider() -> None:
    settings = Settings()

    assert settings.ai_provider == "fake"
    assert settings.app_env == "development"
    assert settings.has_gemini_api_key is False
    assert settings.google_client_id is None
    assert settings.auth_jwt_secret.get_secret_value() not in repr(settings)


def test_settings_reject_non_async_postgres_url() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(database_url="postgresql://localhost/mia")


def test_production_requires_explicit_auth_configuration() -> None:
    with pytest.raises(ValidationError, match="GOOGLE_CLIENT_ID"):
        Settings(app_env="production")


def test_auth_secret_errors_and_repr_never_include_supplied_value() -> None:
    sensitive = "synthetic-short-key"
    with pytest.raises(ValidationError) as error:
        Settings(auth_jwt_secret=sensitive)
    assert sensitive not in str(error.value)
    with pytest.raises(ValidationError, match="AUTH_JWT_SECRET"):
        Settings(app_env="production", google_client_id="test-client")
