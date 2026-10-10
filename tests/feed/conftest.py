"""Feed fixtures: real auth (bearer + web cookie) with the feed router on a Postgres harness."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from app.api.v1.feed import build_feed_router
from app.modules.auth.composition import request_authenticator
from app.modules.conversations.feed import FeedService, FeedTurn
from app.modules.conversations.models import Modality, TurnSource
from httpx import AsyncClient

from tests.workspace.harness import Harness, android_login, build_harness, web_login


@dataclass
class FeedWorld:
    harness: Harness
    feed: FeedService

    async def alice_bearer(
        self, client: AsyncClient, subject: str
    ) -> tuple[dict[str, str], uuid.UUID]:
        headers = await android_login(client, subject)
        me = await client.get("/v1/me", headers=headers)
        return headers, uuid.UUID(me.json()["id"])

    async def web_user(self, client: AsyncClient, subject: str) -> uuid.UUID:
        signed_in = await web_login(client, subject)
        return uuid.UUID(signed_in["user"]["id"])

    async def exchange(
        self,
        user_id: uuid.UUID,
        label: str,
        *,
        source: TurnSource = TurnSource.RUN_CHAT,
        display: dict[str, Any] | None = None,
        modality: Modality = Modality.TEXT,
        actions: tuple[dict[str, Any], ...] = (),
    ) -> tuple[uuid.UUID, FeedTurn]:
        """One user message plus its answer in the primary feed (two entries)."""
        feed_id = await self.feed.primary_feed_id(user_id=user_id)
        run_id = uuid.uuid4()
        turn = await self.feed.open_turn(
            user_id=user_id,
            conversation_id=feed_id,
            run_id=run_id,
            source=source,
            input_text=f"{label}: question",
            modality=modality,
        )
        await self.feed.record_answer(
            user_id=user_id,
            turn=turn,
            text_value=f"{label}: answer",
            response={"speech": f"{label}.", "display": display, "followups": []},
            modality=modality,
            actions=actions,
        )
        return run_id, turn


@pytest.fixture
async def world() -> AsyncIterator[FeedWorld]:
    async with build_harness() as harness:
        service = FeedService(harness.factory)
        harness.app.include_router(
            build_feed_router(service=service, authenticator=request_authenticator(harness.app))
        )
        yield FeedWorld(harness=harness, feed=service)
