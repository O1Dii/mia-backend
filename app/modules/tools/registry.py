"""Typed definitions and a closed registry for server-owned tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.providers.ai.base import ToolDefinition


class StrictToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CurrentDatetimeArguments(StrictToolModel):
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("unknown timezone") from error
        return value


class CurrentDatetimeResult(StrictToolModel):
    local_datetime: str
    timezone: str


ToolHandler = Callable[[BaseModel], Awaitable[BaseModel]]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    definition: ToolDefinition
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler
    timeout_seconds: float = 2.0
    max_output_bytes: int = 2048
    required_scopes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_output_bytes < 1:
            raise ValueError("tool limits must be positive")


class ToolRegistry:
    def __init__(self, tools: tuple[RegisteredTool, ...]) -> None:
        names = [tool.definition.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate tool name")
        self._tools = {tool.definition.name: tool for tool in tools}

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def declarations(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())


class ToolConfirmationPort(Protocol):
    """Future write-tool gate; no implementation is registered in the PoC."""

    async def confirm(
        self, *, user_id: UUID, call_id: UUID, arguments: Mapping[str, object]
    ) -> bool: ...


def current_datetime_registry(*, clock: Callable[[], datetime] | None = None) -> ToolRegistry:
    now = clock or (lambda: datetime.now(UTC))

    async def get_datetime(arguments: BaseModel) -> BaseModel:
        assert isinstance(arguments, CurrentDatetimeArguments)
        local = now().astimezone(ZoneInfo(arguments.timezone))
        return CurrentDatetimeResult(local_datetime=local.isoformat(), timezone=arguments.timezone)

    definition = ToolDefinition(
        name="system.get_current_datetime",
        description="Return the current date and time in an IANA timezone.",
        parameters=CurrentDatetimeArguments.model_json_schema(),
        risk="read",
        requires_confirmation=False,
    )
    return ToolRegistry(
        (RegisteredTool(definition, CurrentDatetimeArguments, CurrentDatetimeResult, get_datetime),)
    )
