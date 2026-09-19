"""Explicit tool policy, bounded execution, and idempotent PostgreSQL audit."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.tools.models import ToolExecution, ToolExecutionStatus
from app.modules.tools.registry import RegisteredTool, ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolPrincipal:
    user_id: UUID
    scopes: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    call_id: UUID
    name: str
    status: str
    result: dict[str, object] | None = None
    error_code: str | None = None
    error_message: str | None = None
    replayed: bool = False

    def event_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "call_id": str(self.call_id),
            "name": self.name,
            "status": self.status,
        }
        if self.result is not None:
            payload["result"] = self.result
        if self.error_code is not None:
            payload["error"] = {
                "code": self.error_code,
                "message": self.error_message or "Tool execution failed.",
            }
        return payload


class ToolExecutor:
    MAX_ARGUMENT_BYTES = 4096

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ToolRegistry,
    ) -> None:
        self._session_factory = session_factory
        self.registry = registry

    @staticmethod
    def _encoded_arguments(arguments: object) -> bytes | None:
        try:
            if not isinstance(arguments, dict):
                return None
            return json.dumps(
                arguments,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return None

    @staticmethod
    def _failure(call_id: UUID, name: str, code: str) -> ToolOutcome:
        messages = {
            "tool_not_found": "Tool is not available.",
            "tool_invalid_arguments": "Tool arguments are invalid.",
            "tool_permission_denied": "Tool is not permitted.",
            "tool_confirmation_required": "Tool requires confirmation.",
            "tool_execution_failed": "Tool execution failed.",
        }
        return ToolOutcome(call_id, name, "failed", error_code=code, error_message=messages[code])

    @staticmethod
    def _policy(
        tool: RegisteredTool | None,
        principal: ToolPrincipal,
        *,
        proposed_risk: str | None,
        proposed_confirmation: bool | None,
    ) -> str | None:
        if tool is None:
            return "tool_not_found"
        definition = tool.definition
        if proposed_risk is not None and proposed_risk != definition.risk:
            return "tool_permission_denied"
        if (
            proposed_confirmation is not None
            and proposed_confirmation != definition.requires_confirmation
        ):
            return "tool_permission_denied"
        if definition.risk != "read":
            return (
                "tool_confirmation_required"
                if definition.requires_confirmation
                else "tool_permission_denied"
            )
        if definition.requires_confirmation:
            return "tool_confirmation_required"
        if not tool.required_scopes.issubset(principal.scopes):
            return "tool_permission_denied"
        return None

    async def _claim(
        self,
        *,
        principal: ToolPrincipal,
        call_id: UUID,
        turn_id: UUID,
        response_id: UUID,
        name: str,
        arguments_hash: str,
    ) -> ToolOutcome | None:
        async with self._session_factory() as session, session.begin():
            statement = (
                insert(ToolExecution)
                .values(
                    id=uuid4(),
                    user_id=principal.user_id,
                    call_id=call_id,
                    turn_id=turn_id,
                    response_id=response_id,
                    name=name,
                    arguments_hash=arguments_hash,
                    status=ToolExecutionStatus.RUNNING,
                )
                .on_conflict_do_nothing(constraint="uq_tool_executions_user_call")
            )
            result = await session.execute(statement)
            if result.rowcount == 1:
                return None
            existing = await session.scalar(
                select(ToolExecution).where(
                    ToolExecution.user_id == principal.user_id,
                    ToolExecution.call_id == call_id,
                )
            )
            if existing is None:
                raise RuntimeError("tool audit claim disappeared")
            if (
                existing.turn_id != turn_id
                or existing.response_id != response_id
                or existing.name != name
                or existing.arguments_hash != arguments_hash
            ):
                return self._failure(call_id, name, "tool_permission_denied")
            if existing.status == ToolExecutionStatus.RUNNING:
                return self._failure(call_id, name, "tool_execution_failed")
            return ToolOutcome(
                call_id=call_id,
                name=name,
                status=existing.status.value,
                result=existing.result,
                error_code=existing.error_code,
                error_message=(
                    self._failure(call_id, name, existing.error_code).error_message
                    if existing.error_code is not None
                    else None
                ),
                replayed=True,
            )

    async def _finish(self, principal: ToolPrincipal, outcome: ToolOutcome) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(ToolExecution)
                .where(
                    ToolExecution.user_id == principal.user_id,
                    ToolExecution.call_id == outcome.call_id,
                )
                .with_for_update()
            )
            if row is None or row.status != ToolExecutionStatus.RUNNING:
                raise RuntimeError("active tool audit claim disappeared")
            row.status = ToolExecutionStatus(outcome.status)
            row.result = outcome.result
            row.error_code = outcome.error_code
            row.finished_at = datetime.now(UTC)

    async def _finish_cancelled(self, principal: ToolPrincipal, outcome: ToolOutcome) -> None:
        """Record the terminal row even when the caller is cancelled again.

        A realtime barge-in cancels the running tool task and the session
        close that follows cancels it once more. Every ``Task.cancel`` is
        delivered, so awaiting the audit write inline aborts it mid
        transaction and leaves the row ``running``. Run the write as its own
        task and wait for it out, then let the cancellation propagate.
        """
        pending = asyncio.ensure_future(self._finish(principal, outcome))
        while True:
            try:
                await asyncio.shield(pending)
                return
            except asyncio.CancelledError:
                if pending.done():
                    raise

    async def execute(
        self,
        *,
        principal: ToolPrincipal,
        call_id: UUID,
        turn_id: UUID,
        response_id: UUID,
        name: str,
        arguments: object,
        proposed_risk: str | None = None,
        proposed_confirmation: bool | None = None,
    ) -> ToolOutcome:
        encoded = self._encoded_arguments(arguments)
        arguments_hash = hashlib.sha256(encoded or b"invalid-arguments").hexdigest()
        replay = await self._claim(
            principal=principal,
            call_id=call_id,
            turn_id=turn_id,
            response_id=response_id,
            name=name,
            arguments_hash=arguments_hash,
        )
        if replay is not None:
            return replay

        tool = self.registry.get(name)
        denied = self._policy(
            tool,
            principal,
            proposed_risk=proposed_risk,
            proposed_confirmation=proposed_confirmation,
        )
        if denied is not None:
            outcome = self._failure(call_id, name, denied)
        elif encoded is None or len(encoded) > self.MAX_ARGUMENT_BYTES:
            outcome = self._failure(call_id, name, "tool_invalid_arguments")
        else:
            assert tool is not None
            try:
                parsed = tool.input_model.model_validate_json(encoded)
            except ValidationError:
                outcome = self._failure(call_id, name, "tool_invalid_arguments")
            else:
                try:
                    async with asyncio.timeout(tool.timeout_seconds):
                        raw_result = await tool.handler(parsed)
                    checked = tool.output_model.model_validate(raw_result)
                    result = checked.model_dump(mode="json")
                    output_bytes = json.dumps(result, ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                    if len(output_bytes) > tool.max_output_bytes:
                        raise ValueError("tool output is too large")
                    outcome = ToolOutcome(call_id, name, "succeeded", result=result)
                except asyncio.CancelledError:
                    await self._finish_cancelled(
                        principal,
                        ToolOutcome(call_id, name, "cancelled", error_code="tool_execution_failed"),
                    )
                    raise
                except Exception:
                    outcome = self._failure(call_id, name, "tool_execution_failed")

        await self._finish(principal, outcome)
        return outcome
