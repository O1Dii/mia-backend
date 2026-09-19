"""Regression for the audit row left ``running`` by a repeated cancellation.

A realtime barge-in cancels the running tool task and the session close that
follows cancels it again. The second ``Task.cancel`` used to abort the audit
write at its first await, leaving a permanently ``running`` row for a call
that never completed. See ``ToolExecutor._finish_cancelled``.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from app.modules.tools import ToolExecutor, ToolPrincipal
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.e2e_tools import harness

pytestmark = [harness.requires_backend, pytest.mark.usefixtures("backend")]


@pytest.mark.parametrize("cancellations", [1, 2, 3])
async def test_cancelled_execution_is_audited_however_often_it_is_cancelled(
    cancellations: int,
) -> None:
    entered = asyncio.Event()
    engine = create_async_engine(harness.database_url())
    try:
        executor = ToolExecutor(
            async_sessionmaker(engine, expire_on_commit=False),
            harness.registry_with_handler(harness.blocking_handler(entered)),
        )
        call_id = uuid4()
        task = asyncio.create_task(
            executor.execute(
                principal=ToolPrincipal(harness.USER_ID),
                call_id=call_id,
                turn_id=harness.TURN_ID,
                response_id=uuid4(),
                name=harness.TOOL_NAME,
                arguments={"timezone": "Europe/Minsk"},
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        for _ in range(cancellations):
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await engine.dispose()

    rows = await harness.audit_rows()
    assert [(row.call_id, row.status, row.result) for row in rows] == [(call_id, "cancelled", None)]
