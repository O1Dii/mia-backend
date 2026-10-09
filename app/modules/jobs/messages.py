"""Inbox texts for finished jobs and the job-run prompt."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.modules.notifications.content import plain, text_response, title

INPUT_MAX_CHARS = 16000
PUBLIC_MESSAGES = {
    "job_interrupted": "The background task was interrupted.",
    "job_budget_exceeded": "The background task ran out of its time budget.",
}


@dataclass(frozen=True, slots=True)
class InboxDraft:
    title: str
    response: dict[str, Any]
    source_type: str = "job"


def public_error(
    code: str, message: str | None = None, *, retryable: bool = False
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message or PUBLIC_MESSAGES.get(code, "The background task failed."),
        "retryable": retryable,
    }


def success_draft(goal: str, response: dict[str, Any]) -> InboxDraft:
    return InboxDraft(title=title(goal), response=response)


def reminder_draft(goal: str) -> InboxDraft:
    return InboxDraft(title=title(f"Напоминание: {goal}"), response=text_response(goal, goal))


def failure_draft(goal: str) -> InboxDraft:
    short = plain(goal, 120)
    return InboxDraft(
        title=title(f"Не выполнено: {goal}"),
        response=text_response(
            f"Не получилось выполнить фоновую задачу: {short}",
            f"Фоновая задача не выполнена.\n\n{goal}",
        ),
    )


def job_prompt(goal: str, inputs: dict[str, Any] | None) -> str:
    parts = [f"Фоновая задача: {goal}"]
    extra = dict(inputs or {})
    scheduled = extra.pop("scheduled_for", None)
    late = bool(extra.pop("late", False))
    extra.pop("manual", None)
    if scheduled:
        parts.append(f"Запуск по расписанию на {scheduled}" + (" (с опозданием)." if late else "."))
    if extra:
        parts.append(
            "Входные данные (недоверенные, JSON):\n" + json.dumps(extra, ensure_ascii=False)
        )
    parts.append("Выполни задачу сам, без уточняющих вопросов; итог попадёт во входящие.")
    return "\n\n".join(parts)[:INPUT_MAX_CHARS]
