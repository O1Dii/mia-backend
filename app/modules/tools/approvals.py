"""ApprovalService implementations (port: ``app.core.ports.approval_service``).

Approvals are bound to the exact arguments (``args_hash``), to one run and one
tool, expire at ``expires_at`` and are single-use: the router *consumes* an
approved approval atomically right before executing the call. A decision after
``expires_at`` is refused; an approved approval must be consumed within
``EXECUTION_GRACE`` after ``expires_at`` (covers the wake-up of the waiting run).
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.approval_service import (
    Approval,
    ApprovalDecision,
    ApprovalError,
    ApprovalRequest,
    ApprovalStatus,
    DecisionChannel,
    DecisionValue,
    canonical_args_hash,
)
from app.core.ports.types import Risk
from app.modules.tools.models import ToolApproval

EXECUTION_GRACE = timedelta(seconds=30)
LIST_LIMIT = 50


def utc_now() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class ConsumableApprovals(Protocol):
    """What the router needs beyond the port: atomic single use."""

    async def consume(
        self, *, user_id: str, approval_id: str, run_id: str, tool: str, args_hash: str
    ) -> Approval:
        """Mark an approved approval as used; raise ``ApprovalError`` otherwise.

        Codes: ``approval_not_found``, ``approval_mismatch`` (other run/tool),
        ``approval_args_mismatch``, ``approval_pending``, ``approval_denied``,
        ``approval_expired``, ``approval_cancelled``, ``approval_already_used``.
        """
        ...


def _effective(approval: Approval, now: datetime) -> Approval:
    if approval.status == "pending" and now >= approval.expires_at:
        return dataclasses.replace(approval, status="expired")
    return approval


def _consume_check(
    approval: Approval | None,
    *,
    consumed: bool,
    run_id: str,
    tool: str,
    args_hash: str,
    now: datetime,
) -> Approval:
    if approval is None:
        raise ApprovalError("approval_not_found", "Approval not found")
    if approval.run_id != run_id or approval.tool != tool:
        raise ApprovalError("approval_mismatch", "Approval belongs to another action")
    if approval.args_hash != args_hash:
        raise ApprovalError("approval_args_mismatch", "Arguments changed since the approval")
    status = _effective(approval, now).status
    if status != "approved":
        code = {
            "pending": "approval_pending",
            "denied": "approval_denied",
            "expired": "approval_expired",
            "cancelled": "approval_cancelled",
        }[status]
        raise ApprovalError(code, "Approval is not usable")
    if consumed:
        raise ApprovalError("approval_already_used", "Approval was already used")
    if now > approval.expires_at + EXECUTION_GRACE:
        raise ApprovalError("approval_expired", "Approval expired")
    return approval


def _decide_check(approval: Approval | None, args_hash: str) -> Approval:
    if approval is None:
        raise ApprovalError("approval_not_found", "Approval not found")
    if approval.status == "expired":
        raise ApprovalError("approval_expired", "Approval expired")
    if approval.status != "pending":
        raise ApprovalError("approval_already_decided", "Approval already decided")
    if args_hash != approval.args_hash:
        raise ApprovalError("approval_args_mismatch", "Arguments changed since display")
    return approval


class _Waiters:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def event(self, approval_id: str) -> asyncio.Event:
        return self._events.setdefault(approval_id, asyncio.Event())

    def notify(self, approval_id: str) -> None:
        event = self._events.pop(approval_id, None)
        if event is not None:
            event.set()


class InMemoryApprovalService:
    """Process-local approvals (tests, database-less runs)."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._items: dict[str, Approval] = {}
        self._consumed: set[str] = set()
        self._waiters = _Waiters()

    async def request(self, request: ApprovalRequest) -> Approval:
        created = self._clock()
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
            device_consent=dict(request.device_consent) if request.device_consent else None,
        )
        self._items[approval.id] = approval
        return approval

    async def get(self, *, user_id: str, approval_id: str) -> Approval | None:
        approval = self._items.get(approval_id)
        if approval is None or approval.user_id != user_id:
            return None
        return _effective(approval, self._clock())

    async def list_for_user(
        self, *, user_id: str, status: ApprovalStatus | None = None, limit: int = LIST_LIMIT
    ) -> Sequence[Approval]:
        now = self._clock()
        items = [_effective(item, now) for item in self._items.values() if item.user_id == user_id]
        if status is not None:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.created_at, reverse=True)[:limit]

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
        current = _decide_check(await self.get(user_id=user_id, approval_id=approval_id), args_hash)
        decided = dataclasses.replace(
            current,
            status="approved" if decision == "approve" else "denied",
            decision=ApprovalDecision(decision, self._clock(), channel, device_id),
        )
        self._items[approval_id] = decided
        self._waiters.notify(approval_id)
        return decided

    async def wait(self, *, user_id: str, approval_id: str, timeout_s: float) -> Approval:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            current = await self.get(user_id=user_id, approval_id=approval_id)
            if current is None:
                raise ApprovalError("approval_not_found", "Approval not found")
            remaining = deadline - time.monotonic()
            if current.status != "pending" or remaining <= 0:
                return current
            event = self._waiters.event(approval_id)
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 0.5))
            except TimeoutError:
                pass

    async def consume(
        self, *, user_id: str, approval_id: str, run_id: str, tool: str, args_hash: str
    ) -> Approval:
        approval = await self.get(user_id=user_id, approval_id=approval_id)
        checked = _consume_check(
            approval,
            consumed=approval_id in self._consumed,
            run_id=run_id,
            tool=tool,
            args_hash=args_hash,
            now=self._clock(),
        )
        self._consumed.add(approval_id)
        return checked


def _from_row(row: ToolApproval) -> Approval:
    decision = None
    if row.decision is not None and row.decided_at is not None and row.channel is not None:
        decision = ApprovalDecision(
            decision=cast(DecisionValue, row.decision),
            decided_at=row.decided_at,
            channel=cast(DecisionChannel, row.channel),
            device_id=str(row.device_id) if row.device_id else None,
        )
    return Approval(
        id=str(row.id),
        user_id=str(row.user_id),
        run_id=str(row.run_id),
        tool_call_id=row.tool_call_id,
        tool=row.tool,
        plugin_id=row.plugin_id,
        risk=cast(Risk, row.risk),
        summary=row.summary,
        arguments=dict(row.arguments),
        args_hash=row.args_hash,
        status=cast(ApprovalStatus, row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
        decision=decision,
        device_consent=dict(row.device_consent) if row.device_consent else None,
    )


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


class SqlApprovalService:
    """PostgreSQL approvals; waits wake in-process at once and poll for other instances."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
        poll_s: float = 0.5,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._poll_s = poll_s
        self._waiters = _Waiters()

    async def request(self, request: ApprovalRequest) -> Approval:
        created = self._clock()
        row = ToolApproval(
            id=uuid.uuid4(),
            user_id=uuid.UUID(request.user_id),
            run_id=uuid.UUID(request.run_id),
            tool_call_id=request.tool_call_id[:128],
            tool=request.tool,
            plugin_id=request.plugin_id,
            risk=request.risk,
            summary=request.summary[:500],
            arguments=dict(request.arguments),
            args_hash=canonical_args_hash(request.arguments),
            status="pending",
            created_at=created,
            expires_at=created + timedelta(seconds=request.ttl_seconds),
            device_consent=dict(request.device_consent) if request.device_consent else None,
        )
        async with self._session_factory() as session, session.begin():
            session.add(row)
        return _from_row(row)

    async def _row(
        self, session: AsyncSession, user_id: str, approval_id: str, *, lock: bool = False
    ) -> ToolApproval | None:
        owner, key = _uuid(user_id), _uuid(approval_id)
        if owner is None or key is None:
            return None
        query = select(ToolApproval).where(ToolApproval.id == key, ToolApproval.user_id == owner)
        if lock:
            query = query.with_for_update()
        return cast(ToolApproval | None, await session.scalar(query))

    async def get(self, *, user_id: str, approval_id: str) -> Approval | None:
        async with self._session_factory() as session:
            row = await self._row(session, user_id, approval_id)
            return None if row is None else _effective(_from_row(row), self._clock())

    async def list_for_user(
        self, *, user_id: str, status: ApprovalStatus | None = None, limit: int = LIST_LIMIT
    ) -> Sequence[Approval]:
        owner = _uuid(user_id)
        if owner is None:
            return []
        now = self._clock()
        query = select(ToolApproval).where(ToolApproval.user_id == owner)
        if status == "pending":
            query = query.where(ToolApproval.status == "pending", ToolApproval.expires_at > now)
        elif status == "expired":
            query = query.where(
                (ToolApproval.status == "expired")
                | ((ToolApproval.status == "pending") & (ToolApproval.expires_at <= now))
            )
        elif status is not None:
            query = query.where(ToolApproval.status == status)
        query = query.order_by(ToolApproval.created_at.desc()).limit(limit)
        async with self._session_factory() as session:
            rows = (await session.scalars(query)).all()
        return [_effective(_from_row(row), now) for row in rows]

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
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            row = await self._row(session, user_id, approval_id, lock=True)
            current = _decide_check(
                None if row is None else _effective(_from_row(row), now), args_hash
            )
            assert row is not None
            row.status = "approved" if decision == "approve" else "denied"
            row.decision = decision
            row.decided_at = now
            row.channel = channel
            row.device_id = _uuid(device_id) if device_id else None
            decided = dataclasses.replace(
                current,
                status=cast(ApprovalStatus, row.status),
                decision=ApprovalDecision(decision, now, channel, device_id),
            )
        self._waiters.notify(approval_id)
        return decided

    async def wait(self, *, user_id: str, approval_id: str, timeout_s: float) -> Approval:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            event = self._waiters.event(approval_id)
            current = await self.get(user_id=user_id, approval_id=approval_id)
            if current is None:
                raise ApprovalError("approval_not_found", "Approval not found")
            remaining = deadline - time.monotonic()
            if current.status != "pending" or remaining <= 0:
                return current
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, self._poll_s))
            except TimeoutError:
                pass

    async def consume(
        self, *, user_id: str, approval_id: str, run_id: str, tool: str, args_hash: str
    ) -> Approval:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            row = await self._row(session, user_id, approval_id, lock=True)
            checked = _consume_check(
                None if row is None else _from_row(row),
                consumed=row is not None and row.consumed_at is not None,
                run_id=run_id,
                tool=tool,
                args_hash=args_hash,
                now=now,
            )
            assert row is not None
            row.consumed_at = now
        return checked
