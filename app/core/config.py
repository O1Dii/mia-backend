"""Typed runtime configuration with safe defaults for local fake-mode work."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables."""

    model_config = SettingsConfigDict(
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "mia-backend"
    log_level: str = "INFO"
    ai_provider: Literal["fake", "gemini"] = "fake"
    database_url: str = "postgresql+asyncpg://mia:mia-local-only@localhost:5432/mia"
    redis_url: str = "redis://localhost:6379/0"
    gemini_api_key: SecretStr | None = Field(default=None, repr=False)
    google_client_id: str | None = Field(default=None, min_length=1)
    auth_jwt_secret: SecretStr = Field(default=SecretStr("mia-local-auth-signing-key-change-me"))
    auth_access_token_ttl_seconds: int = Field(default=900, ge=600, le=900)
    auth_refresh_token_ttl_days: int = Field(default=30, ge=1, le=180)
    auth_nonce_ttl_seconds: int = Field(default=300, ge=60, le=900)
    auth_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    auth_nonce_rate_limit: int = Field(default=10, ge=1, le=1000)
    auth_exchange_rate_limit: int = Field(default=10, ge=1, le=1000)
    auth_refresh_rate_limit: int = Field(default=30, ge=1, le=1000)
    auth_authenticated_rate_limit: int = Field(default=120, ge=1, le=5000)

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            message = "LOG_LEVEL must be a standard Python logging level"
            raise ValueError(message)
        return normalized

    @field_validator("database_url")
    @classmethod
    def require_async_postgres_url(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            message = "DATABASE_URL must use the postgresql+asyncpg scheme"
            raise ValueError(message)
        return value

    @field_validator("redis_url")
    @classmethod
    def require_redis_url(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://")):
            message = "REDIS_URL must use the redis or rediss scheme"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def require_production_auth_configuration(self) -> Settings:
        if len(self.auth_jwt_secret.get_secret_value().encode()) < 32:
            raise ValueError("AUTH_JWT_SECRET must contain at least 32 bytes")
        if self.app_env in {"staging", "production"}:
            if self.google_client_id is None:
                raise ValueError("GOOGLE_CLIENT_ID is required outside local and test environments")
            if self.auth_jwt_secret.get_secret_value() == "mia-local-auth-signing-key-change-me":
                raise ValueError("AUTH_JWT_SECRET must be overridden outside local environments")
        return self

    @property
    def has_gemini_api_key(self) -> bool:
        """Expose only whether an optional provider credential is configured."""
        return self.gemini_api_key is not None


@lru_cache
def get_settings() -> Settings:
    """Return one immutable settings instance for the process."""
    return Settings()
