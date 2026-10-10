"""Inbox texts for finished jobs, the job-run prompt and the single result of a job.

A job has exactly one result (M74-R1): its run's final answer. Whatever the job's model passed
to ``notify`` meanwhile (``jobs.notes``) is folded into that answer instead of being delivered
on its own, so the user gets one card, one inbox item and one notification.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
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


def notes_markdown(notes: Sequence[dict[str, Any]] | None) -> str:
    """The ``notify`` texts of a job, in call order (a heading in bold above its text)."""
    parts: list[str] = []
    for note in notes or ():
        text_value = str(note.get("text") or "").strip()
        if not text_value:
            continue
        heading = str(note.get("title") or "").strip()
        parts.append(f"**{heading}**\n\n{text_value}" if heading else text_value)
    return "\n\n".join(parts)


_WORDS = re.compile(r"\w+")


def _words(value: str) -> str:
    return " ".join(_WORDS.findall(value.casefold()))


def with_notes(response: dict[str, Any], notes: Sequence[dict[str, Any]] | None) -> dict[str, Any]:
    """The job's result: its final answer with the ``notify`` texts folded in.

    - no notes: the answer as it is;
    - a text/card answer: the notes above its markdown (unless the answer already contains them);
    - a speech-only answer («Отчёт готов, уведомление отправлено»): it only announces the notes,
      so the notes are the screen part and the spoken line is taken from them;
    - an app/link answer keeps its display (the notes cannot be merged into it).
    """
    body = notes_markdown(notes)
    if not body:
        return response
    display = response.get("display")
    if display is None:
        spoken = " ".join(
            f"{note['title']}. {note['text']}" if note.get("title") else str(note.get("text"))
            for note in notes or ()
            if str(note.get("text") or "").strip()
        )
        folded = text_response(spoken, body)
        return {**folded, "followups": list(response.get("followups") or [])}
    if not isinstance(display, dict) or display.get("kind") not in ("text", "card"):
        return response
    markdown = str(display.get("markdown") or "")
    if _words(body) in _words(markdown):
        return response
    merged = f"{body}\n\n{markdown}".strip()[:32000]
    return {**response, "display": {**display, "markdown": merged}}


def reminder_draft(goal: str) -> InboxDraft:
    return InboxDraft(title=title(f"Напоминание: {goal}"), response=text_response(goal, goal))


def failure_draft(goal: str, notes: Sequence[dict[str, Any]] | None = None) -> InboxDraft:
    short = plain(goal, 120)
    body = notes_markdown(notes)
    done = f"\n\nЧто успела подготовить:\n\n{body}" if body else ""
    return InboxDraft(
        title=title(f"Не выполнено: {goal}"),
        response=text_response(
            f"Не получилось выполнить фоновую задачу: {short}",
            f"Фоновая задача не выполнена.\n\n{goal}{done}",
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
    parts.append(
        "Выполни задачу сама, без уточняющих вопросов. Твой финальный ответ и есть результат: "
        "Mia сама доставит его пользователю один раз (во входящие и уведомлением), даже если в "
        "задаче просят прислать или сообщить. Не вызывай для этого notify."
    )
    return "\n\n".join(parts)[:INPUT_MAX_CHARS]
