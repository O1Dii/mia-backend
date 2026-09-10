"""Typed runtime configuration with safe defaults for local fake-mode work."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables."""

    model_config = SettingsConfigDict(
        extra="ignore",
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "mia-backend"
    log_level: str = "INFO"
    ai_provider: Literal["fake", "gemini"] = "fake"
    database_url: str = "postgresql+asyncpg://mia:mia-local-only@localhost:5432/mia"
    redis_url: str = "redis://localhost:6379/0"
    google_api_key: SecretStr | None = Field(default=None, repr=False)

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

    @property
    def has_google_api_key(self) -> bool:
        """Expose only whether an optional provider credential is configured."""
        return self.google_api_key is not None


@lru_cache
def get_settings() -> Settings:
    """Return one immutable settings instance for the process."""
    return Settings()
