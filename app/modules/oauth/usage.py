"""Spend journal and the monthly per-app limit (Review §8.1 ``llm.generate``).

Spend is the provider-reported ``cost_usd`` of each model step; when a provider reports
no price (e.g. a ChatGPT plan), Mia estimates it from tokens with the configured
per-million rates and marks the row ``estimated``. The limit is checked before a
request and after every model step, so a request can overshoot by at most one step.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.oauth.models import OAuthAppUsage
from app.modules.oauth.service import AppAccess, utc_now

Surface = Literal["chat_completions", "responses", "mcp"]
_MICRO = Decimal("0.000001")


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class Pricing:
    input_usd_per_mtok: Decimal = Decimal("0.50")
    output_usd_per_mtok: Decimal = Decimal("2.00")

    def cost(
        self, input_tokens: int, output_tokens: int, reported: float | None
    ) -> tuple[Decimal, bool]:
        if reported is not None:
            return Decimal(str(reported)).quantize(_MICRO), False
        estimate = (
            Decimal(input_tokens) * self.input_usd_per_mtok
            + Decimal(output_tokens) * self.output_usd_per_mtok
        ) / Decimal(1_000_000)
        return estimate.quantize(_MICRO), True


@dataclass(frozen=True, slots=True)
class UsageEntry:
    at: datetime
    surface: str
    run_id: uuid.UUID | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    estimated: bool


@dataclass
class UsageLedger:
    sessions: async_sessionmaker[AsyncSession]
    pricing: Pricing = field(default_factory=Pricing)
    clock: Callable[[], datetime] = field(default=utc_now)

    async def month_spend(self, grant_id: uuid.UUID) -> Decimal:
        async with self.sessions() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(OAuthAppUsage.cost_usd), 0)).where(
                    OAuthAppUsage.grant_id == grant_id,
                    OAuthAppUsage.at >= month_start(self.clock()),
                )
            )
        return Decimal(total or 0)

    async def remaining(self, access: AppAccess) -> Decimal:
        return access.monthly_limit_usd - await self.month_spend(access.grant_id)

    async def record(
        self,
        access: AppAccess,
        *,
        surface: Surface,
        run_id: uuid.UUID | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Decimal,
        estimated: bool,
    ) -> None:
        async with self.sessions() as session, session.begin():
            session.add(
                OAuthAppUsage(
                    id=uuid.uuid4(),
                    grant_id=access.grant_id,
                    user_id=access.user_id,
                    client_id=access.client_id,
                    run_id=run_id,
                    surface=surface,
                    at=self.clock(),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd.quantize(_MICRO),
                    estimated=estimated,
                )
            )

    async def entries(self, grant_id: uuid.UUID, *, limit: int = 100) -> list[UsageEntry]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(OAuthAppUsage)
                    .where(OAuthAppUsage.grant_id == grant_id)
                    .order_by(OAuthAppUsage.at.desc(), OAuthAppUsage.id.desc())
                    .limit(limit)
                )
            ).all()
        return [
            UsageEntry(
                r.at, r.surface, r.run_id, r.input_tokens, r.output_tokens, r.cost_usd, r.estimated
            )
            for r in rows
        ]
