"""Typed runtime configuration with safe defaults for local fake-mode work."""

from __future__ import annotations

import re
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
    gemini_text_model: str | None = Field(default=None, min_length=1)
    gemini_live_model: str | None = Field(default=None, min_length=1)
    gemini_request_timeout_seconds: float = Field(default=30.0, gt=0)
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
    voice_enabled: bool = True
    text_enabled: bool = True
    max_live_session_seconds: int = Field(default=900, ge=30, le=3600)
    max_daily_text_turns: int = Field(default=100, ge=1, le=10000)
    max_daily_live_sessions: int = Field(default=20, ge=1, le=1000)
    max_active_live_sessions_per_user: int = Field(default=1, ge=1, le=4)
    provider_failure_threshold: int = Field(default=5, ge=1, le=100)
    provider_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    metrics_enabled: bool = False
    # M11 model gateway. Master keys: "id:base64url" entries separated by commas; the last is
    # active unless CREDENTIALS_MASTER_KEY_ACTIVE names one. Required in staging/production.
    credentials_master_keys: SecretStr | None = Field(default=None, repr=False)
    credentials_master_key_active: str | None = Field(default=None, min_length=1)
    model_request_timeout_seconds: float = Field(default=60.0, gt=0)
    model_stream_deadline_seconds: float = Field(default=120.0, gt=0)
    model_usage_pause_seconds: int = Field(default=3600, ge=60, le=86400)

    # Run engine profiles (M12, ADR-0011 §1). Steps are model calls per run.
    run_voice_max_steps: int = Field(default=3, ge=1, le=10)
    run_voice_max_tool_calls: int = Field(default=2, ge=0, le=10)
    run_voice_timeout_seconds: float = Field(default=25.0, gt=0, le=120)
    run_voice_speech_max_chars: int = Field(default=250, ge=40, le=300)
    run_voice_ack_delay_ms: int = Field(default=1500, ge=0, le=10000)
    run_speech_ack_text: str = Field(default="Секунду.", min_length=1, max_length=120)
    run_chat_max_steps: int = Field(default=12, ge=1, le=50)
    run_chat_max_tool_calls: int = Field(default=24, ge=0, le=100)
    run_chat_timeout_seconds: float = Field(default=150.0, gt=0, le=600)
    run_job_max_steps: int = Field(default=40, ge=1, le=200)
    run_job_max_tool_calls: int = Field(default=80, ge=0, le=500)
    run_job_timeout_seconds: float = Field(default=1800.0, gt=0, le=1800)
    run_app_max_steps: int = Field(default=8, ge=1, le=50)
    run_app_max_tool_calls: int = Field(default=8, ge=0, le=100)
    run_app_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    run_context_recent_entries: int = Field(default=20, ge=2, le=200)
    run_feed_summary_batch: int = Field(default=20, ge=1, le=500)
    tools_call_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    tools_list_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    tools_list_cache_seconds: float = Field(default=60.0, ge=0, le=3600)
    tools_approval_ttl_seconds: int = Field(default=120, ge=10, le=3600)
    tools_max_result_chars: int = Field(default=8000, ge=256, le=64000)
    tools_mcp_host_allowlist: str | None = Field(default=None, max_length=8000)
    """Comma-separated hosts; when set, remote MCP hosts must also be listed here."""
    public_base_url: str = Field(default="http://localhost:8000", min_length=8, max_length=2048)
    """Externally visible origin of this API; the OAuth callback of plugin connections (M15)."""
    store_admin_user_ids: str | None = Field(default=None, max_length=4000)
    """Comma-separated ``users.id`` allowed to publish plugin releases (M15)."""
    store_packages_dir: str | None = Field(default=None, max_length=1024)
    """Where published packages (``SKILL.md``, ``ui/``) are materialized; temp dir if unset."""
    connections_oauth_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    connections_oauth_flow_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    # Telegram bot (M41): link (deep link /start <token>), webhook, notify channel.
    telegram_bot_token: SecretStr | None = Field(default=None, repr=False)
    """Bot API token (BotFather); without it the bot, its webhook and the channel stay off."""
    telegram_bot_username: str | None = Field(
        default=None, pattern=r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$"
    )
    """Bot username for deep links ``https://t.me/<username>?start=<token>``."""
    telegram_webhook_secret: SecretStr | None = Field(default=None, repr=False)
    """Checked against ``X-Telegram-Bot-Api-Secret-Token`` (1-256 of ``A-Za-z0-9_-``)."""
    telegram_api_base: str = Field(default="https://api.telegram.org", min_length=8, max_length=256)
    telegram_request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    telegram_link_ttl_seconds: int = Field(default=900, ge=60, le=86400)

    @field_validator("telegram_webhook_secret")
    @classmethod
    def telegram_secret_alphabet(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{1,256}", value.get_secret_value()
        ):
            message = "TELEGRAM_WEBHOOK_SECRET must be 1-256 characters of A-Z a-z 0-9 _ -"
            raise ValueError(message)
        return value

    oauth_issuer: str | None = Field(default=None, min_length=8, max_length=2048)
    """Issuer of «Войти через Mia» (M50); defaults to ``PUBLIC_BASE_URL``."""
    oauth_consent_url: str | None = Field(default=None, min_length=8, max_length=2048)
    """Consent page (web ``oauth.html``); defaults to ``<PUBLIC_BASE_URL>/oauth.html``."""
    oauth_access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    oauth_refresh_token_ttl_days: int = Field(default=30, ge=1, le=180)
    oauth_code_ttl_seconds: int = Field(default=60, ge=10, le=600)
    oauth_request_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    oauth_cimd_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    oauth_cimd_cache_seconds: int = Field(default=3600, ge=60, le=86400)
    oauth_allow_loopback_clients: bool = False
    """Dev only: accept ``http://localhost`` client_id documents (refused in staging/production)."""
    apps_default_monthly_limit_usd: float = Field(default=5.0, ge=0, le=1000)
    apps_max_monthly_limit_usd: float = Field(default=100.0, ge=0, le=100000)
    apps_estimated_input_usd_per_mtok: float = Field(default=0.5, ge=0, le=1000)
    apps_estimated_output_usd_per_mtok: float = Field(default=2.0, ge=0, le=1000)

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
        if self.ai_provider == "gemini":
            missing = [
                name
                for name, value in (
                    ("GEMINI_API_KEY", self.gemini_api_key),
                    ("GEMINI_TEXT_MODEL", self.gemini_text_model),
                    ("GEMINI_LIVE_MODEL", self.gemini_live_model),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"{', '.join(missing)} required when AI_PROVIDER=gemini")
        if self.app_env in {"staging", "production"}:
            if self.google_client_id is None:
                raise ValueError("GOOGLE_CLIENT_ID is required outside local and test environments")
            if self.auth_jwt_secret.get_secret_value() == "mia-local-auth-signing-key-change-me":
                raise ValueError("AUTH_JWT_SECRET must be overridden outside local environments")
            if self.credentials_master_keys is None:
                raise ValueError("CREDENTIALS_MASTER_KEYS is required outside local environments")
            if self.oauth_allow_loopback_clients:
                raise ValueError("OAUTH_ALLOW_LOOPBACK_CLIENTS is for local development only")
        return self

    @property
    def has_gemini_api_key(self) -> bool:
        """Expose only whether an optional provider credential is configured."""
        return self.gemini_api_key is not None


@lru_cache
def get_settings() -> Settings:
    """Return one immutable settings instance for the process."""
    return Settings()
