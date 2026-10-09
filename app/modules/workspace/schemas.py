"""HTTP models for `/v1/workspace`, `/v1/installations*` and `/v1/dashboard*`.

Field names follow `contracts/mvp/v1/openapi.yaml` (`Installation`,
`CreateInstallationRequest`, `Dashboard`); extra response fields are additive.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

TileSize = Literal["1x1", "2x1", "2x2", "4x2", "4x4"]
InstallationStatus = Literal["active", "needs_connection", "needs_consent", "disabled"]
MAX_JSON_BYTES = 64 * 1024
MAX_TILES = 60
PLUGIN_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,127}$"


def _bounded_json(value: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_JSON_BYTES:
        raise ValueError("JSON object exceeds 64 KiB")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceView(BaseModel):
    id: UUID
    name: str
    owner_user_id: UUID
    layout_version: int


class InstallationView(BaseModel):
    id: UUID
    plugin_id: str
    version: str
    status: InstallationStatus
    custom_url: str | None
    source: Literal["catalog", "custom", "legacy_web"]
    grants: list[str]
    tools_digest: str | None
    state_version: int
    """Pass back as ``expected_version`` to ``PATCH /v1/installations/{id}``."""
    created_at: datetime
    updated_at: datetime


class InstallationList(BaseModel):
    items: list[InstallationView]


class CatalogInstallRequest(Strict):
    plugin_id: str = Field(pattern=PLUGIN_ID_PATTERN)
    version: str = Field(min_length=1, max_length=64)
    tools_digest: str = Field(min_length=1, max_length=128)


class CustomInstallRequest(Strict):
    custom_mcp_url: str = Field(pattern=r"^https://", max_length=2048)


class PatchInstallationRequest(Strict):
    expected_version: int = Field(ge=0)
    status: InstallationStatus | None = None
    grants: list[str] | None = Field(default=None, max_length=64)


class PluginStateView(BaseModel):
    plugin_id: str
    data: dict[str, Any]
    state_version: int


class PutPluginStateRequest(Strict):
    expected_version: int = Field(ge=0)
    data: dict[str, Any]

    _limit = field_validator("data")(_bounded_json)


class TilePlacement(Strict):
    size: TileSize
    position: int = Field(ge=0, le=10_000)


class DashboardTileInput(Strict):
    id: UUID
    installation_id: UUID
    tile_id: str = Field(min_length=1, max_length=64)
    size: TileSize
    """Desktop size (contract field)."""
    position: int = Field(ge=0, le=10_000)
    """Desktop position (contract field)."""
    mobile: TilePlacement | None = None
    """Mobile placement; defaults to the desktop one."""
    settings: dict[str, Any] | None = None

    @field_validator("settings")
    @classmethod
    def limit_settings(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _bounded_json(value)


class DashboardTileView(BaseModel):
    id: UUID
    installation_id: UUID
    tile_id: str
    size: TileSize
    position: int
    mobile: TilePlacement
    settings: dict[str, Any]
    state_version: int


class DashboardView(BaseModel):
    expected_version: int
    """Current layout version; send it back unchanged with the next ``PUT``."""
    tiles: list[DashboardTileView]


class PutDashboardRequest(Strict):
    expected_version: int = Field(ge=0)
    tiles: list[DashboardTileInput] = Field(max_length=MAX_TILES)


class PatchTileRequest(Strict):
    expected_version: int = Field(ge=0)
    settings: dict[str, Any]

    _limit = field_validator("settings")(_bounded_json)
