"""Configuration owned by the Gemini Live adapter."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from pydantic import SecretStr

from app.providers.ai.base import ToolDefinition

_MIA_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True, slots=True)
class GeminiLiveConfig:
    """Provider settings whose representation cannot expose the API key."""

    api_key: SecretStr
    model: str
    system_instruction: str
    model_alias: str = "realtime-default"
    request_timeout_seconds: float = 30.0
    idle_timeout_seconds: float = 90.0
    max_output_tokens: int = 1024
    output_frame_bytes: int = 4096
    context_window_trigger_tokens: int = 25_000
    context_window_target_tokens: int = 8_000
    reconnect_attempts: int = 2
    tools: tuple[ToolDefinition, ...] = ()

    def __post_init__(self) -> None:
        if not self.api_key.get_secret_value():
            raise ValueError("Gemini API key must be configured")
        if not self.model:
            raise ValueError("Gemini Live model must be configured")
        if not self.system_instruction:
            raise ValueError("Gemini Live system instruction must be configured")
        if not self.model_alias:
            raise ValueError("Gemini Live model alias must be configured")
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                self.request_timeout_seconds,
                self.idle_timeout_seconds,
            )
        ):
            raise ValueError("Gemini Live timeouts must be positive")
        if self.max_output_tokens < 1:
            raise ValueError("Gemini Live max output tokens must be positive")
        if self.output_frame_bytes < 2 or self.output_frame_bytes % 2:
            raise ValueError("Gemini Live output frame limit must align with PCM16 samples")
        if self.context_window_target_tokens < 1:
            raise ValueError("Gemini Live context target must be positive")
        if self.context_window_trigger_tokens <= self.context_window_target_tokens:
            raise ValueError("Gemini Live context trigger must exceed its target")
        if self.reconnect_attempts < 0:
            raise ValueError("Gemini Live reconnect attempts cannot be negative")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("Gemini Live tool names must be unique")
        if any(_MIA_TOOL_NAME.fullmatch(name) is None for name in names):
            raise ValueError("Gemini Live tool names must follow the MIA namespaced format")
