"""Configuration tests."""

import pytest
from app.core.config import Settings
from pydantic import ValidationError


def test_settings_default_to_fake_provider() -> None:
    settings = Settings()

    assert settings.ai_provider == "fake"
    assert settings.app_env == "development"


def test_settings_reject_non_async_postgres_url() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(database_url="postgresql://localhost/mia")
