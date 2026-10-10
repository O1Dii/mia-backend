"""Core tools the model always has (Review §5.2(d)): background.*, schedule.*, notify.

Registered on M13's ``CoreToolRegistry``; the router validates arguments against these
schemas, applies risk policy and audits every call. Jobs and schedules never start
other jobs (``background.start``/``schedule.*`` are not offered to ``job`` runs).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.ports.tool_router import ToolContext
from app.core.ports.types import JsonObject, RunProfile
from app.modules.jobs.domain import Job, JobError, OriginType, Schedule
from app.modules.jobs.service import JobService
from app.modules.runs.domain import RunFinishedError
from app.modules.runs.service import RunService
from app.modules.tools.core_tools import CoreTool, CoreToolRegistry, CoreToolResult

logger = logging.getLogger(__name__)

_CHANNELS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["channels"],
    "properties": {
        "channels": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "enum": ["push", "telegram", "inbox"]},
        }
    },
}
_RULE = {
    "type": "string",
    "maxLength": 1000,
    "pattern": (
        r"^(DTSTART[^\n]*\n)?RRULE:FREQ=(MINUTELY|HOURLY|DAILY|WEEKLY|MONTHLY|YEARLY)"
        r"(;[A-Z]+=[A-Za-z0-9,:+-]+)*$"
    ),
    "description": "RFC 5545 RRULE, e.g. RRULE:FREQ=DAILY;BYHOUR=8;BYMINUTE=30",
}
_TIMEZONE = {
    "type": "string",
    "pattern": r"^(UTC|[A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})$",
    "description": "IANA time zone of the user, e.g. Europe/Moscow",
}
_ID = {"type": "string", "format": "uuid", "minLength": 36, "maxLength": 36}

START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["goal"],
    "properties": {
        "goal": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
            "description": "The task in the user's own words, as they asked for it.",
        },
        "inputs": {"type": "object"},
        "delivery": _CHANNELS,
        "budget": {
            "type": "object",
            "additionalProperties": False,
            "required": ["max_steps", "max_duration_seconds"],
            "properties": {
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 200},
                "max_duration_seconds": {"type": "integer", "minimum": 1, "maximum": 1800},
                "max_cost_usd": {"type": "number", "minimum": 0},
            },
        },
    },
}
JOB_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"job_id": _ID},
}
JOB_ID_REQUIRED: dict[str, Any] = {**JOB_ID_SCHEMA, "required": ["job_id"]}
SCHEDULE_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rule", "timezone", "goal"],
    "properties": {
        "rule": _RULE,
        "timezone": _TIMEZONE,
        "goal": {"type": "string", "minLength": 1, "maxLength": 4000},
        "delivery": _CHANNELS,
        "misfire_policy": {
            "type": "string",
            "enum": ["skip", "run_once", "limited_catch_up"],
            "default": "skip",
        },
        "catch_up_limit": {"type": "integer", "minimum": 1, "maximum": 24},
        "kind": {
            "type": "string",
            "enum": ["run", "reminder"],
            "description": "reminder: send goal as the notification text without running a model",
        },
    },
}
SCHEDULE_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schedule_id"],
    "properties": {"schedule_id": _ID},
}
EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": {}}
NOTIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "channels": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "enum": ["push", "telegram", "inbox"]},
        },
    },
}

STATUS_RU = {
    "queued": "в очереди",
    "running": "выполняется",
    "waiting_approval": "ждёт подтверждения",
    "succeeded": "готова",
    "failed": "не выполнена",
    "cancelled": "отменена",
}


def _failed(code: str, text: str) -> CoreToolResult:
    return CoreToolResult(content=text, status="failed", error_code=code)


def _job_line(job: Job) -> str:
    line = f"{job.id} «{job.goal[:80]}»: {STATUS_RU.get(job.status, job.status)}"
    if job.status == "succeeded" and job.result:
        line += f". Итог: {str(job.result.get('speech', ''))[:300]}"
    return line


def _schedule_line(schedule: Schedule) -> str:
    following = schedule.to_wire()["next_run_at"] or "нет"
    return (
        f"{schedule.id} «{schedule.goal[:80]}» {schedule.rule.splitlines()[-1]} "
        f"({schedule.timezone}), {schedule.status}, следующий запуск {following}"
    )


def _error_text(error: JobError) -> CoreToolResult:
    texts = {
        "job_not_found": "Задача не найдена.",
        "schedule_not_found": "Расписание не найдено.",
        "job_limit_exceeded": "Слишком много активных фоновых задач. Дождитесь или отмените.",
        "schedule_limit_exceeded": "Слишком много расписаний. Удалите ненужные.",
        "invalid_schedule_rule": "Правило расписания некорректно: нужен RRULE с будущим запуском.",
        "invalid_timezone": "Неизвестный часовой пояс.",
        "schedule_interval_too_short": "Слишком частое расписание.",
        "notify_rate_limited": "Слишком много уведомлений за час.",
    }
    return _failed(error.code, texts.get(error.code, "Операция не выполнена."))


class JobTools:
    def __init__(self, service: JobService, runs: RunService | None = None) -> None:
        self.service = service
        self.runs = runs

    @staticmethod
    def _user(context: ToolContext) -> uuid.UUID:
        return uuid.UUID(context.user_id)

    async def start(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        origin_type: OriginType = "app" if context.profile == "app" else "run"
        origin_id = context.app_client_id or context.run_id
        try:
            job, _ = await self.service.create_job(
                user_id=self._user(context),
                request=dict(args),
                origin_type=origin_type,
                origin_id=str(origin_id),
            )
        except JobError as error:
            return _error_text(error)
        if self.runs is not None:
            try:
                await self.runs.publish(
                    run_id=uuid.UUID(context.run_id),
                    event_type="job.created",
                    payload={"job_id": str(job.id), "goal": job.goal},
                )
            except (RunFinishedError, ValueError):
                logger.info("job_created_event_skipped")
        return CoreToolResult(
            content=(
                f"Фоновая задача поставлена в очередь, id {job.id}. Результат придёт во "
                "входящие и уведомлением. Скажи пользователю коротко: «Запустила, сообщу»."
            ),
            structured_content={"job": job.to_wire()},
        )

    async def status(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        user = self._user(context)
        job_id = args.get("job_id")
        if job_id is None:
            jobs, _ = await self.service.list_jobs(user_id=user, limit=5)
            if not jobs:
                return CoreToolResult(content="Фоновых задач нет.", structured_content={"jobs": []})
            return CoreToolResult(
                content="; ".join(_job_line(job) for job in jobs),
                structured_content={"jobs": [job.to_wire() for job in jobs]},
            )
        try:
            job = await self.service.get_job(user_id=user, job_id=uuid.UUID(str(job_id)))
        except JobError as error:
            return _error_text(error)
        return CoreToolResult(content=_job_line(job), structured_content={"job": job.to_wire()})

    async def cancel(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        try:
            job = await self.service.cancel_job(
                user_id=self._user(context), job_id=uuid.UUID(str(args["job_id"]))
            )
        except JobError as error:
            return _error_text(error)
        text = (
            f"Задача «{job.goal[:80]}» уже {STATUS_RU.get(job.status, job.status)}."
            if job.terminal and job.status != "cancelled"
            else f"Задача «{job.goal[:80]}» отменяется."
        )
        return CoreToolResult(content=text, structured_content={"job": job.to_wire()})

    async def schedule_create(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        try:
            schedule, _ = await self.service.create_schedule(
                user_id=self._user(context), request=dict(args)
            )
        except JobError as error:
            return _error_text(error)
        return CoreToolResult(
            content="Расписание создано: " + _schedule_line(schedule),
            structured_content={"schedule": schedule.to_wire()},
        )

    async def schedule_list(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        schedules = await self.service.list_schedules(user_id=self._user(context))
        if not schedules:
            return CoreToolResult(content="Расписаний нет.", structured_content={"schedules": []})
        return CoreToolResult(
            content="; ".join(_schedule_line(schedule) for schedule in schedules),
            structured_content={"schedules": [schedule.to_wire() for schedule in schedules]},
        )

    async def _set_status(
        self, context: ToolContext, args: JsonObject, status: str
    ) -> CoreToolResult:
        try:
            schedule = await self.service.update_schedule(
                user_id=self._user(context),
                schedule_id=uuid.UUID(str(args["schedule_id"])),
                changes={"status": status},
            )
        except JobError as error:
            return _error_text(error)
        done = "приостановлено" if status == "paused" else "возобновлено"
        return CoreToolResult(
            content=f"Расписание {done}: " + _schedule_line(schedule),
            structured_content={"schedule": schedule.to_wire()},
        )

    async def schedule_pause(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        return await self._set_status(context, args, "paused")

    async def schedule_resume(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        return await self._set_status(context, args, "active")

    async def schedule_run_now(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        try:
            job = await self.service.run_schedule_now(
                user_id=self._user(context), schedule_id=uuid.UUID(str(args["schedule_id"]))
            )
        except JobError as error:
            return _error_text(error)
        return CoreToolResult(
            content=f"Запущено вне расписания, задача {job.id}.",
            structured_content={"job": job.to_wire()},
        )

    async def schedule_delete(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        try:
            await self.service.delete_schedule(
                user_id=self._user(context), schedule_id=uuid.UUID(str(args["schedule_id"]))
            )
        except JobError as error:
            return _error_text(error)
        return CoreToolResult(content="Расписание удалено.")

    async def notify(self, context: ToolContext, args: JsonObject) -> CoreToolResult:
        user = self._user(context)
        source_type: Any = "system"
        source_id = f"run:{context.run_id}"
        owner = await self.service.store.job_for_run(uuid.UUID(context.run_id))
        if owner is not None:
            source_type, source_id = "job", str(owner.id)
        channels = args.get("channels")
        try:
            item = await self.service.notify(
                event_type="job_result" if owner is not None else "plugin_message",
                user_id=user,
                text=str(args["text"]),
                channels=[str(c) for c in channels] if isinstance(channels, list) else None,
                heading=str(args["title"]) if args.get("title") else None,
                source_type=source_type,
                source_id=source_id,
            )
        except JobError as error:
            return _error_text(error)
        return CoreToolResult(
            content="Уведомление отправлено.", structured_content={"inbox_item": item.to_wire()}
        )


def job_core_tools(tools: JobTools) -> list[CoreTool]:
    interactive: frozenset[RunProfile] = frozenset({"voice", "chat"})
    return [
        CoreTool(
            "background.start",
            "Start a background task (profile job) ONLY for long work (over a minute), deferred "
            "work, or when the user explicitly asks for it. Do NOT use it for questions you can "
            "answer in this chat with a few tool calls (search, news, short summaries): answer "
            "directly instead. `goal` is the user's request in the user's own words. The result "
            "arrives in the inbox and in the channels the user chose in settings; pass `delivery` "
            "only if the user names a channel. Returns the job id.",
            START_SCHEMA,
            "write_reversible",
            tools.start,
            voice=True,
            profiles=interactive,
        ),
        CoreTool(
            "background.status",
            "Status of one background job by job_id, or of the latest jobs without it.",
            JOB_ID_SCHEMA,
            "read",
            tools.status,
            voice=True,
            profiles=interactive,
        ),
        CoreTool(
            "background.cancel",
            "Cancel a background job by job_id.",
            JOB_ID_REQUIRED,
            "write_reversible",
            tools.cancel,
            voice=True,
            profiles=interactive,
        ),
        CoreTool(
            "schedule.create",
            "Create a recurring schedule: RRULE + IANA timezone + goal. kind=reminder just "
            "sends goal as the notification; misfire_policy decides what happens with "
            "occurrences missed while Mia was down (skip, run_once, limited_catch_up).",
            SCHEDULE_CREATE_SCHEMA,
            "write_reversible",
            tools.schedule_create,
            voice=True,
            profiles=interactive,
        ),
        CoreTool(
            "schedule.list",
            "List the user's schedules with ids and next run times.",
            EMPTY_SCHEMA,
            "read",
            tools.schedule_list,
            voice=True,
            profiles=interactive,
        ),
        CoreTool(
            "schedule.pause",
            "Pause a schedule by schedule_id.",
            SCHEDULE_ID_SCHEMA,
            "write_reversible",
            tools.schedule_pause,
            voice=True,
            profiles=interactive,
        ),
        CoreTool(
            "schedule.resume",
            "Resume a paused schedule by schedule_id (missed occurrences are not replayed).",
            SCHEDULE_ID_SCHEMA,
            "write_reversible",
            tools.schedule_resume,
            voice=True,
            profiles=interactive,
        ),
        CoreTool(
            "schedule.run_now",
            "Run a schedule's goal once right now, outside its timing.",
            SCHEDULE_ID_SCHEMA,
            "write_reversible",
            tools.schedule_run_now,
            voice=True,
            profiles=interactive,
        ),
        CoreTool(
            "schedule.delete",
            "Delete a schedule by schedule_id.",
            SCHEDULE_ID_SCHEMA,
            "write_reversible",
            tools.schedule_delete,
            voice=True,
            profiles=interactive,
        ),
        CoreTool(
            "notify",
            "Notify the user: the inbox and the channels the user chose in settings (push, the "
            "Telegram bot, ...); pass `channels` only if the user names one.",
            NOTIFY_SCHEMA,
            "write_reversible",
            tools.notify,
            profiles=frozenset[RunProfile]({"chat", "job"}),
        ),
    ]


def register_job_tools(registry: CoreToolRegistry, tools: JobTools) -> None:
    for tool in job_core_tools(tools):
        registry.register(tool)
