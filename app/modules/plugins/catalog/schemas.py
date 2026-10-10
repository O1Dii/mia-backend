"""HTTP models of the store: catalog entries, consent screen, reviews, install updates."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.workspace.schemas import PLUGIN_ID_PATTERN


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsentTool(BaseModel):
    name: str
    description: str
    risk: Literal["read", "write_reversible", "write_external"]
    voice: bool


class ConsentGrant(BaseModel):
    id: str
    text: str


class ConsentLlm(BaseModel):
    via_host: bool
    monthly_budget_usd: float | None


class ConsentView(BaseModel):
    """What the install screen shows: tools and their risk, data, network and model use."""

    plugin_id: str
    version: str
    tools_digest: str
    tools: list[ConsentTool]
    grants: list[ConsentGrant]
    network: list[str]
    public_web: bool
    llm: ConsentLlm
    memory: list[str]
    notify: bool
    schedules: bool
    device_tools: list[str]
    requires_connection: bool
    stores_state: bool = True


class RatingView(BaseModel):
    average: float | None
    count: int


class CatalogEntryView(BaseModel):
    plugin_id: str
    version: str
    manifest: dict[str, Any]
    name: str
    description: str
    category: str
    author: str | None
    kind: Literal["builtin", "remote", "declarative"]
    tagline: str | None = None
    color: str | None = None
    art: str | None = None
    featured: bool = False
    rating: RatingView
    installs: int
    icon_url: str | None
    screenshots: list[str]
    published_at: datetime


class CatalogEntryDetail(CatalogEntryView):
    versions: list[str]
    consent: ConsentView


class CatalogPage(BaseModel):
    items: list[CatalogEntryView]
    next_cursor: str | None


class ReviewView(BaseModel):
    id: UUID
    plugin_id: str
    rating: int
    text: str
    author: str
    mine: bool
    updated_at: datetime


class ReviewList(BaseModel):
    items: list[ReviewView]
    rating: RatingView


class PutReviewRequest(Strict):
    rating: int = Field(ge=1, le=5)
    text: str = Field(default="", max_length=2000)


class InstallRequest(Strict):
    """Consent to a catalog plugin: the digest and grants of the screen the user saw."""

    version: str = Field(min_length=1, max_length=64)
    tools_digest: str = Field(min_length=1, max_length=128)
    grants: list[str] = Field(max_length=128)


class UpdateInfo(BaseModel):
    installation_id: UUID
    plugin_id: str
    installed_version: str
    latest_version: str
    update_available: bool
    added_grants: list[str]
    removed_grants: list[str]
    digest_changed: bool
    consent: ConsentView | None
    """The consent screen of the latest version (when an update is available)."""


class UpgradeRequest(Strict):
    expected_version: int = Field(ge=0)
    version: str = Field(min_length=1, max_length=64)
    tools_digest: str = Field(min_length=1, max_length=128)
    grants: list[str] = Field(max_length=128)


class DiscoveredTool(BaseModel):
    name: str
    description: str
    risk: Literal["write_external"] = "write_external"
    selected: bool = False
    """Ticked on the consent screen before the user changes anything (M15-R1)."""


class DiscoverResult(BaseModel):
    """Result of probing an MCP server added by URL."""

    installation_id: UUID
    state: Literal["needs_connection", "ready"]
    """``needs_connection``: the server wants OAuth; ``ready``: tools can be consented."""
    tools: list[DiscoveredTool]
    tools_digest: str | None
    grants: list[ConsentGrant]
    host: str
    skipped_tools: list[str] = Field(default_factory=list)
    """Tools whose names the manifest grammar cannot hold; Mia does not offer them."""
    tool_limit: int = Field(default=0, ge=0)
    """How many tools this server may enable next to the user's other plugins."""


class ConsentCustomRequest(Strict):
    expected_version: int = Field(ge=0)
    tools_digest: str = Field(min_length=1, max_length=128)
    grants: list[str] = Field(max_length=128)
    tools: list[str] | None = Field(default=None, max_length=1024)
    """Tools to enable; omitted: every usable tool (refused when they exceed ``tool_limit``)."""


class InstallationHealth(BaseModel):
    """Whether Mia can offer an installation's tools right now (M15-R1)."""

    installation_id: UUID
    plugin_id: str
    state: Literal[
        "ok", "limited", "needs_attention", "needs_connection", "needs_consent", "disabled"
    ]
    """``limited``: only ``tools`` of ``declared_tools`` fit the budget; ``needs_attention``:
    nothing is offered, see ``problem``."""
    problem: (
        Literal[
            "manifest_invalid",
            "manifest_missing",
            "manifest_mismatch",
            "tool_budget",
            "tools_changed",
            "server_unavailable",
        ]
        | None
    ) = None
    reason: str | None = None
    """Content-free detail of ``problem`` (``too_many_tools``, ``timeout``, …)."""
    tools: int = Field(ge=0)
    """Tools Mia offers the model from this installation."""
    declared_tools: int = Field(ge=0)
    """Tools the installation enabled (its manifest)."""
    tool_limit: int = Field(ge=0)
    """Tools this installation may enable next to the user's other plugins."""


PluginIdParam = PLUGIN_ID_PATTERN
