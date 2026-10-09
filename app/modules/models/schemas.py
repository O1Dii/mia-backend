"""API schemas for model connections. Keys are accepted on write and never returned."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.modules.models.connections import ConnectionView

ProviderName = Literal[
    "openai", "deepinfra", "groq", "mistral", "local", "anthropic", "gemini", "ollama"
]
CapabilityName = Literal["tools", "chat_only"]


class CreateConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    label: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=160)
    api_key: SecretStr | None = Field(default=None, max_length=512, repr=False)
    base_url: str | None = Field(default=None, max_length=512)
    capability: CapabilityName | None = None
    make_primary: bool | None = None


class UpdateConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=160)
    api_key: SecretStr | None = Field(default=None, max_length=512, repr=False)
    base_url: str | None = Field(default=None, max_length=512)
    capability: CapabilityName | None = None
    make_primary: bool | None = None


class ConnectionResponse(BaseModel):
    """Public connection: ``key_last4`` is the only trace of a key."""

    id: uuid.UUID
    provider: str
    label: str
    model: str
    base_url: str | None
    capability: CapabilityName
    has_key: bool
    key_last4: str | None
    status: Literal["active", "paused", "needs_reauth"]
    paused_until: datetime | None
    is_primary: bool
    verified: bool
    last_tested_at: datetime | None
    last_test_ok: bool | None
    last_test_error_code: str | None
    created_at: datetime

    @classmethod
    def of(cls, view: ConnectionView) -> ConnectionResponse:
        return cls.model_validate(view, from_attributes=True)


class ConnectionListResponse(BaseModel):
    items: list[ConnectionResponse]


class TestResultResponse(BaseModel):
    __test__ = False

    ok: bool
    latency_ms: int
    error_code: str | None


class SignInStartResponse(BaseModel):
    attempt_id: str
    authorize_url: str
    expires_at: datetime


class SignInStatusResponse(BaseModel):
    attempt_id: str
    status: Literal["pending", "completed", "failed"]
    error_code: str | None
    connection_id: uuid.UUID | None
