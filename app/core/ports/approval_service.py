"""ApprovalService port: approvals bound to exact arguments (Review §5.3).

Schema: ``contracts/mvp/v1/approval.schema.json``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from app.core.ports.types import JsonObject, PortError, Risk

ApprovalStatus = Literal["pending", "approved", "denied", "expired", "cancelled"]
DecisionValue = Literal["approve", "deny"]
DecisionChannel = Literal["web", "android", "voice_trusted"]
ApprovalErrorCode = Literal[
    "approval_not_found",
    "approval_args_mismatch",
    "approval_expired",
    "approval_already_decided",
]

DEFAULT_APPROVAL_TTL_SECONDS = 120


def canonical_args_hash(arguments: JsonObject) -> str:
    """Return ``sha256:<hex>`` of canonical JSON arguments.

    Canonical form: keys sorted, separators ``(",", ":")``, non-ASCII kept
    as-is, UTF-8 encoded. Clients that display an approval echo this value.
    """
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalError(PortError):
    """Raised by ``decide``; ``code`` is an ``ApprovalErrorCode``."""


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    user_id: str
    run_id: str
    tool_call_id: str
    tool: str
    risk: Risk
    summary: str
    """Human-readable action rendered by the core."""
    arguments: JsonObject
    plugin_id: str | None = None
    ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS
    device_consent: JsonObject | None = None
    """Set when only the target device may decide (ADR-0016, M70): ``DeviceConsent`` of
    ``contracts/mvp/v1/device-access.schema.json``."""


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    decision: DecisionValue
    decided_at: datetime
    channel: DecisionChannel
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    id: str
    user_id: str
    run_id: str
    tool_call_id: str
    tool: str
    plugin_id: str | None
    risk: Risk
    summary: str
    arguments: JsonObject
    args_hash: str
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decision: ApprovalDecision | None = None
    device_consent: JsonObject | None = None
    """``DeviceConsent`` (M70): the approval can be decided only on that device."""


@runtime_checkable
class ApprovalService(Protocol):
    async def request(self, request: ApprovalRequest) -> Approval:
        """Create a pending approval; ``args_hash`` is ``canonical_args_hash``."""
        ...

    async def get(self, *, user_id: str, approval_id: str) -> Approval | None:
        """Return the approval if it belongs to the user. A pending approval past
        ``expires_at`` is reported as ``expired``."""
        ...

    async def decide(
        self,
        *,
        user_id: str,
        approval_id: str,
        decision: DecisionValue,
        args_hash: str,
        channel: DecisionChannel,
        device_id: str | None = None,
    ) -> Approval:
        """Record a decision. Raises ``ApprovalError`` when the approval is
        missing or foreign, the hash differs, it expired, or it was decided."""
        ...

    async def wait(self, *, user_id: str, approval_id: str, timeout_s: float) -> Approval:
        """Wait until the approval leaves ``pending`` or ``timeout_s`` elapses and
        return its current state."""
        ...
