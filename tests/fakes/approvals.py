"""In-memory ApprovalService with an injectable clock."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.ports.approval_service import (
    Approval,
    ApprovalDecision,
    ApprovalError,
    ApprovalRequest,
    DecisionChannel,
    DecisionValue,
    canonical_args_hash,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class FakeApprovalService:
    def __init__(self, *, now: Callable[[], datetime] = utc_now) -> None:
        self._now = now
        self._approvals: dict[str, Approval] = {}
        self._changed: dict[str, asyncio.Event] = {}

    async def request(self, request: ApprovalRequest) -> Approval:
        created = self._now()
        approval = Approval(
            id=str(uuid.uuid4()),
            user_id=request.user_id,
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            tool=request.tool,
            plugin_id=request.plugin_id,
            risk=request.risk,
            summary=request.summary,
            arguments=dict(request.arguments),
            args_hash=canonical_args_hash(request.arguments),
            status="pending",
            created_at=created,
            expires_at=created + timedelta(seconds=request.ttl_seconds),
        )
        self._approvals[approval.id] = approval
        self._changed[approval.id] = asyncio.Event()
        return approval

    async def get(self, *, user_id: str, approval_id: str) -> Approval | None:
        approval = self._approvals.get(approval_id)
        if approval is None or approval.user_id != user_id:
            return None
        return self._expire_if_due(approval)

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
        approval = await self.get(user_id=user_id, approval_id=approval_id)
        if approval is None:
            raise ApprovalError("approval_not_found", "Approval not found")
        if approval.status == "expired":
            raise ApprovalError("approval_expired", "Approval expired")
        if approval.status != "pending":
            raise ApprovalError("approval_already_decided", "Approval already decided")
        if args_hash != approval.args_hash:
            raise ApprovalError("approval_args_mismatch", "Arguments changed since display")
        decided = dataclasses.replace(
            approval,
            status="approved" if decision == "approve" else "denied",
            decision=ApprovalDecision(
                decision=decision, decided_at=self._now(), channel=channel, device_id=device_id
            ),
        )
        self._store(decided)
        return decided

    async def wait(self, *, user_id: str, approval_id: str, timeout_s: float) -> Approval:
        approval = await self.get(user_id=user_id, approval_id=approval_id)
        if approval is None:
            raise ApprovalError("approval_not_found", "Approval not found")
        if approval.status == "pending":
            try:
                await asyncio.wait_for(self._changed[approval_id].wait(), timeout_s)
            except TimeoutError:
                pass
        current = await self.get(user_id=user_id, approval_id=approval_id)
        assert current is not None
        return current

    async def cancel(self, *, approval_id: str) -> None:
        """Test helper: the run ended before a decision."""
        approval = self._approvals[approval_id]
        if approval.status == "pending":
            self._store(dataclasses.replace(approval, status="cancelled"))

    def _expire_if_due(self, approval: Approval) -> Approval:
        if approval.status == "pending" and self._now() >= approval.expires_at:
            approval = dataclasses.replace(approval, status="expired")
            self._store(approval)
        return approval

    def _store(self, approval: Approval) -> None:
        self._approvals[approval.id] = approval
        self._changed[approval.id].set()
