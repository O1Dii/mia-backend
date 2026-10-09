"""Value types shared by the MVP core ports (ADR-0010…0012, contracts/mvp/v1).

These types are deliberately framework-free: modules map them to their own
persistence and API models. Literal values mirror the JSON Schemas in
``contracts/mvp/v1`` and must change together with them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

JsonObject = Mapping[str, object]
"""A JSON object: tool arguments, ``structuredContent``, input schemas."""

RunProfile = Literal["voice", "chat", "job", "app"]
"""Run profile, Review §5.2(a); ``common.schema.json#/$defs/runProfile``."""

Risk = Literal["read", "write_reversible", "write_external"]
"""Tool risk class, Review §5.3; ``common.schema.json#/$defs/risk``."""

DEFAULT_MODEL: Final = "mia:default"
"""Model reference that resolves to the user's active (primary) model."""


class PortError(Exception):
    """Base error raised by port implementations.

    ``code`` is a stable snake_case machine code safe to expose publicly;
    ``message`` must never contain provider payloads, secrets or user content.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
