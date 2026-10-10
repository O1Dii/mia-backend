"""Jobs and chats (ADR-0017 §4): a job started from a chat answers in that chat too.

The source chat of a job is the conversation of the run that called ``background.start``
(``origin_type = run``). When the job finishes, its result is posted into that chat as a
card (:func:`app.modules.conversations.chats.append_card`) in the same transaction as the
inbox item; schedules, apps and first-party ``POST /v1/jobs`` have no chat and reach only
the shared surfaces. A result for the Telegram chat is also delivered to Telegram.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.conversations.chats import LISTED_KINDS, append_card
from app.modules.conversations.models import ChatKind, Conversation
from app.modules.jobs.messages import InboxDraft
from app.modules.jobs.models import JobRow
from app.modules.runs.models import RunRow


@dataclass(frozen=True, slots=True)
class SourceChat:
    conversation_id: uuid.UUID
    kind: str


def origin_run_id(origin_type: str, origin_id: str) -> uuid.UUID | None:
    if origin_type != "run":
        return None
    try:
        return uuid.UUID(origin_id)
    except ValueError:
        return None


async def source_chats(
    session: AsyncSession, user_id: uuid.UUID, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, SourceChat]:
    """Origin run id → the chat it ran in (only chats that still exist)."""
    if not run_ids:
        return {}
    rows = await session.execute(
        select(RunRow.id, Conversation.id, Conversation.kind)
        .join(Conversation, Conversation.id == RunRow.conversation_id)
        .where(
            RunRow.id.in_(run_ids),
            RunRow.user_id == user_id,
            Conversation.user_id == user_id,
            Conversation.kind.in_(LISTED_KINDS),
        )
    )
    return {run_id: SourceChat(chat_id, str(kind)) for run_id, chat_id, kind in rows}


def card_text(response: dict[str, Any]) -> str:
    """What the chat (and the model's history) shows for a result."""
    display = response.get("display")
    if isinstance(display, dict):
        for key in ("markdown", "text"):
            value = display.get(key)
            if isinstance(value, str) and value.strip():
                return value
    speech = response.get("speech")
    return speech if isinstance(speech, str) and speech.strip() else ""


async def post_result_card(
    session: AsyncSession, row: JobRow, draft: InboxDraft, now: datetime
) -> SourceChat | None:
    """Post the job's result card into its source chat; returns the chat when posted."""
    run_id = origin_run_id(row.origin_type, row.origin_id)
    if run_id is None:
        return None
    chat = (await source_chats(session, row.user_id, [run_id])).get(run_id)
    if chat is None:
        return None
    text_value = card_text(draft.response) or draft.title
    posted = await append_card(
        session,
        user_id=row.user_id,
        conversation_id=chat.conversation_id,
        key=f"job:{row.id}",
        text_value=text_value,
        response=draft.response,
        card={
            "type": "job_result",
            "job_id": str(row.id),
            "status": row.status,
            "title": draft.title,
        },
        now=now,
    )
    return chat if posted else None


def delivery_channels(row: JobRow, chat: SourceChat | None) -> tuple[str, ...]:
    channels = tuple(row.delivery.get("channels") or ())
    if chat is not None and chat.kind == ChatKind.TELEGRAM.value and "telegram" not in channels:
        channels = (*channels, "telegram")
    return channels
