"""Fail-closed per-user provider budgets backed by atomic Redis counters."""

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.core.metrics import METRICS

logger = logging.getLogger(__name__)


class CounterStore(Protocol):
    async def reserve_daily_budget(
        self, counter_key: str, request_key: str, *, ttl_seconds: int, limit: int
    ) -> tuple[bool, int]: ...


class BudgetUnavailableError(RuntimeError):
    """The counter store could not answer; callers must reject the request."""


def record_admission_rejection(reason: str, *, retry_after_seconds: int | None = None) -> None:
    """Count and log one rejected provider admission with a fixed reason token."""
    METRICS.inc("mia_admission_rejections_total", {"reason": reason})
    logger.warning(
        "admission_rejected",
        extra={"reason": reason, "retry_after_seconds": retry_after_seconds},
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DailyBudget:
    """Charge each idempotent request once per user and UTC day."""

    def __init__(self, store: CounterStore, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._store = store
        self._clock = clock

    async def spend(
        self, kind: str, user_id: UUID, *, limit: int, idempotency_key: str
    ) -> tuple[bool, int]:
        now = self._clock()
        next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        counter_key = f"budget:{kind}:{now.date().isoformat()}:{user_id}"
        request_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        try:
            return await self._store.reserve_daily_budget(
                counter_key,
                f"{counter_key}:request:{request_hash}",
                ttl_seconds=max(1, int((next_day - now).total_seconds())),
                limit=limit,
            )
        except Exception as exc:
            raise BudgetUnavailableError("daily budget store is unavailable") from exc
