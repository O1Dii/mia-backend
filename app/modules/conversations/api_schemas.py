"""Strict REST v1 schemas for conversation history and text streaming."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.conversations.models import (
    DeliveryStatus,
    EntryKind,
    EntryRole,
    Modality,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateConversationRequest(StrictModel):
    title: str | None = Field(default=None, max_length=200)


class UpdateConversationRequest(StrictModel):
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def require_a_change(self) -> UpdateConversationRequest:
        if not self.model_fields_set:
            raise ValueError("at least one conversation field is required")
        if "archived" in self.model_fields_set and self.archived is None:
            raise ValueError("archived must be a boolean")
        return self


class SendMessageRequest(StrictModel):
    text: str = Field(min_length=1, max_length=32_000)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class ConversationPageResponse(StrictModel):
    items: tuple[ConversationResponse, ...]
    next_cursor: str | None


class EntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    turn_id: UUID
    sequence: int
    role: EntryRole
    kind: EntryKind
    modality: Modality
    content: dict[str, Any]
    delivery_status: DeliveryStatus
    created_at: datetime


class EntryPageResponse(StrictModel):
    items: tuple[EntryResponse, ...]
    next_cursor: str | None
