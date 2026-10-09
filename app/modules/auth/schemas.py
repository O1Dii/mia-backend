"""HTTP request and response models for the v1 auth contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr


class DeviceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    platform: Literal["android"]
    app_version: str = Field(min_length=1, max_length=64)
    device_name: str | None = Field(default=None, max_length=128)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class GoogleAuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_token: str = Field(min_length=1, max_length=16384)
    nonce: str = Field(min_length=32, max_length=512)
    device: DeviceInput


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=32, max_length=1024)


class AuthNonceResponse(BaseModel):
    nonce: str
    expires_at: datetime


class UserResponse(BaseModel):
    id: UUID
    email: str
    email_verified: bool
    display_name: str | None
    avatar_url: AnyHttpUrl | None
    locale: str
    timezone: str
    created_at: datetime
    updated_at: datetime


class AuthSessionResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    user: UserResponse


class WebGoogleSignInRequest(BaseModel):
    """Google Identity Services credential; the nonce comes from the HttpOnly cookie."""

    model_config = ConfigDict(extra="forbid")

    credential: SecretStr = Field(min_length=1, max_length=16384)


class WebSignInResponse(BaseModel):
    """The session itself travels only in ``Set-Cookie``."""

    user: UserResponse
    expires_at: datetime
