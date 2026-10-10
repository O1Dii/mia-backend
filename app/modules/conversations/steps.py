"""Tool steps and approvals of an answer, for the chat history (``FeedItem.steps``, M74-R1).

Live clients see the steps of an answer from ``tool.call.*`` and ``approval.*`` events. The
history keeps them too, so after a reload or on another device the user still sees which
tools ran, why one failed and who confirmed what (e.g. «решено на телефоне»). Steps are read
when the feed is read: one query for the tool events of all runs of the page, one for their
approvals, one for the runs of job result cards; nothing is stored twice.

``arguments`` and ``error_detail`` are exactly those of ``tool.call.started/completed``
(redacted by M73 before they were stored).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

STEP_LIMIT = 32
"""At most this many steps per answer, in call order."""
STEP_STATUSES = frozenset(
    {"running", "succeeded", "failed", "denied", "expired", "uncertain", "cancelled"}
)
_SETTLED_APPROVAL = {"denied": "denied", "expired": "expired", "cancelled": "cancelled"}
_EVENTS = ("tool.call.started", "tool.call.completed")

Steps = tuple[dict[str, Any], ...]


def _text(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _approval(row: Any, step_status: str) -> dict[str, Any]:
    status = str(row.status)
    if status == "pending" and step_status in _SETTLED_APPROVAL:
        # The run gave up waiting (expiry/cancel) before the approval row was swept.
        status = _SETTLED_APPROVAL[step_status]
    body: dict[str, Any] = {"id": str(row.id), "status": status, "summary": row.summary}
    if row.decided_at is not None:
        body["decided_at"] = row.decided_at.isoformat().replace("+00:00", "Z")
    if row.channel is not None:
        body["channel"] = row.channel
    if row.device_id is not None:
        body["device_id"] = str(row.device_id)
    return body


def _started(payload: dict[str, Any]) -> dict[str, Any] | None:
    call_id, tool = payload.get("tool_call_id"), payload.get("tool")
    if not isinstance(call_id, str) or not isinstance(tool, str):
        return None
    step: dict[str, Any] = {"tool_call_id": call_id, "tool": tool, "status": "running"}
    for key in ("plugin_id", "risk", "executor"):
        if isinstance(payload.get(key), str):
            step[key] = payload[key]
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        step["arguments"] = arguments
    return step


def _complete(step: dict[str, Any], payload: dict[str, Any]) -> None:
    status = payload.get("status")
    if status not in STEP_STATUSES or status == "running":
        return
    step["status"] = status
    if status == "succeeded":
        return
    error = payload.get("error")
    code = _text(error.get("code") if isinstance(error, dict) else None, 64)
    if code is not None:
        step["error_code"] = code
    detail = _text(payload.get("error_detail"), 300)
    if detail is not None:
        step["error_detail"] = detail


async def steps_of_runs(
    session: AsyncSession, user_id: uuid.UUID, run_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Steps]:
    """``run_id`` → its tool steps (with approvals), only runs of ``user_id`` that had any.

    A call without a ``tool.call.completed`` event in a finished answer is ``uncertain``.
    """
    # Imported here: the run engine imports this package (conversations.feed).
    from app.modules.runs.models import RunEventRow, RunRow
    from app.modules.tools.models import ToolApproval

    wanted = list(dict.fromkeys(run_ids))
    if not wanted:
        return {}
    events = await session.execute(
        select(RunEventRow.run_id, RunEventRow.type, RunEventRow.payload)
        .join(RunRow, RunRow.id == RunEventRow.run_id)
        .where(
            RunEventRow.run_id.in_(wanted),
            RunRow.user_id == user_id,
            RunEventRow.type.in_(_EVENTS),
        )
        .order_by(RunEventRow.run_id, RunEventRow.sequence)
    )
    calls: dict[uuid.UUID, dict[str, dict[str, Any]]] = {}
    for run_id, event_type, payload in events:
        if not isinstance(payload, dict):
            continue
        steps = calls.setdefault(run_id, {})
        if event_type == "tool.call.started":
            step = _started(payload)
            if step is not None and step["tool_call_id"] not in steps:
                steps[step["tool_call_id"]] = step
        else:
            found = steps.get(str(payload.get("tool_call_id")))
            if found is not None:
                _complete(found, payload)
    if not calls:
        return {}
    approvals: dict[tuple[uuid.UUID, str], Any] = {}
    rows = await session.execute(
        select(
            ToolApproval.run_id,
            ToolApproval.tool_call_id,
            ToolApproval.id,
            ToolApproval.status,
            ToolApproval.summary,
            ToolApproval.decided_at,
            ToolApproval.channel,
            ToolApproval.device_id,
        )
        .where(ToolApproval.run_id.in_(list(calls)), ToolApproval.user_id == user_id)
        .order_by(ToolApproval.created_at)
    )
    for row in rows:
        approvals[(row.run_id, row.tool_call_id)] = row  # the latest one wins
    result: dict[uuid.UUID, Steps] = {}
    for run_id, steps in calls.items():
        listed: list[dict[str, Any]] = []
        for step in list(steps.values())[:STEP_LIMIT]:
            if step["status"] == "running":
                step["status"] = "uncertain"
            approval = approvals.get((run_id, step["tool_call_id"]))
            if approval is not None:
                step["approval"] = _approval(approval, step["status"])
            listed.append(step)
        if listed:
            result[run_id] = tuple(listed)
    return result


async def job_runs(
    session: AsyncSession, user_id: uuid.UUID, job_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """``job_id`` → the run that produced its result (for the steps of a result card)."""
    from app.modules.jobs.models import JobRow

    wanted = list(dict.fromkeys(job_ids))
    if not wanted:
        return {}
    rows = await session.execute(
        select(JobRow.id, JobRow.run_id).where(
            JobRow.id.in_(wanted), JobRow.user_id == user_id, JobRow.run_id.is_not(None)
        )
    )
    return {job_id: run_id for job_id, run_id in rows if run_id is not None}
