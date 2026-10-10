"""«Пока вас не было»: the next interactive run on the primary feed hears about new inbox items.

:class:`AwaySummaryFeed` wraps the run engine's feed (``RunEngine.feed``) and appends a short
note to the context summary the first time unread, unannounced inbox items exist. Job runs
(started by the worker inside :func:`suppress_away_summary`) and runs on a non-primary
conversation never consume the note.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

from app.modules.conversations.feed import FeedContext, FeedSummarizer, FeedTurn
from app.modules.conversations.models import Modality, TurnSource, TurnStatus
from app.modules.notifications.inbox import InboxItem, InboxService
from app.modules.runs.engine import RunFeed

_SUPPRESSED: ContextVar[bool] = ContextVar("mia_away_summary_suppressed", default=False)


@contextlib.contextmanager
def suppress_away_summary() -> Iterator[None]:
    """Runs started inside this block (their tasks copy the context) skip the note."""
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


def away_note(items: Sequence[InboxItem], total: int) -> str:
    titles = "; ".join(item.title for item in items)
    more = f" и ещё {total - len(items)}" if total > len(items) else ""
    return (
        f"Пока пользователя не было, во входящих появилось: {titles}{more}. "
        "В начале ответа коротко скажи об этом одной фразой («Пока вас не было: …»)."
    )


class AwaySummaryFeed:
    def __init__(self, inner: RunFeed, inbox: InboxService, *, max_items: int = 3) -> None:
        self.inner = inner
        self._inbox = inbox
        self._max_items = max_items

    async def owns_conversation(self, *, user_id: uuid.UUID, conversation_id: uuid.UUID) -> bool:
        return await self.inner.owns_conversation(user_id=user_id, conversation_id=conversation_id)

    async def context(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID | None = None
    ) -> FeedContext:
        context = await self.inner.context(user_id=user_id, conversation_id=conversation_id)
        if _SUPPRESSED.get():
            return context
        items, total = await self._inbox.take_unannounced(user_id=user_id, limit=self._max_items)
        if not items:
            return context
        note = away_note(items, total)
        summary = f"{context.summary.strip()}\n\n{note}" if context.summary else note
        return replace(context, summary=summary)

    async def open_turn(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        source: TurnSource,
        input_text: str,
        modality: Modality = Modality.TEXT,
    ) -> FeedTurn:
        return await self.inner.open_turn(
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            source=source,
            input_text=input_text,
            modality=modality,
        )

    async def record_answer(
        self,
        *,
        user_id: uuid.UUID,
        turn: FeedTurn,
        text_value: str,
        response: dict[str, Any],
        modality: Modality = Modality.TEXT,
        actions: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        await self.inner.record_answer(
            user_id=user_id,
            turn=turn,
            text_value=text_value,
            response=response,
            modality=modality,
            actions=actions,
        )

    async def close_turn(self, *, turn: FeedTurn, status: TurnStatus) -> None:
        await self.inner.close_turn(turn=turn, status=status)

    async def summarize_if_needed(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID, summarizer: FeedSummarizer
    ) -> bool:
        return await self.inner.summarize_if_needed(
            user_id=user_id, conversation_id=conversation_id, summarizer=summarizer
        )
