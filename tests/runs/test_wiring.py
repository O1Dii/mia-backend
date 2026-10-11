"""Configuration, composition and the public error-code registry of the run engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.database import Database
from app.core.redis import RedisClient
from app.core.runtime import Runtime
from app.main import create_app
from app.modules.runs import RunLimits, RunService
from app.modules.runs.composition import install_runs
from app.modules.runs.defaults import NoToolRouter
from app.modules.runs.errors import RUN_CODES, public_error, tool_error
from fastapi import FastAPI

from tests.fakes import FakeModelGateway

REGISTRY = Path("contracts/mvp/v1/error-codes.json")


def test_profile_limits_come_from_settings() -> None:
    settings = Settings(
        app_env="test",
        run_voice_max_steps=2,
        run_voice_max_tool_calls=1,
        run_voice_max_parallel_reads=2,
        run_voice_ack_delay_ms=0,
        run_chat_timeout_seconds=90,
    )
    limits = RunLimits.from_settings(settings)

    assert (limits.voice.max_steps, limits.voice.max_tool_calls) == (2, 1)
    assert limits.voice.max_parallel_reads == 2 and limits.chat.max_parallel_reads == 1
    assert limits.voice.ack_delay_ms is None and limits.voice.speech_max_chars == 250
    assert limits.chat.timeout_s == 90 and limits.chat.stream_deltas
    defaults = RunLimits.from_settings(Settings(app_env="test"))
    assert (defaults.voice.max_steps, defaults.voice.max_tool_calls) == (3, 2)
    assert defaults.voice.max_parallel_reads == 4  # ADR-0011 amendment 2026-10-11
    assert defaults.voice.ack_delay_ms == 1500 and defaults.chat.max_steps == 12
    assert defaults.job.timeout_s == 1800
    with pytest.raises(ValueError):
        Settings(app_env="test", run_voice_speech_max_chars=301)


def test_error_codes_are_registered_in_the_contract() -> None:
    registry = {entry["code"]: entry for entry in json.loads(REGISTRY.read_text("utf-8"))}
    for code, known in RUN_CODES.items():
        entry = registry[code]
        assert (entry["retryable"], entry["public_message"], entry["http_status"]) == (
            known.retryable,
            known.message,
            known.http_status,
        )
    assert public_error("something_else")["code"] == "run_internal_error"
    assert tool_error("failed", "Not A Code!")["code"] == "tool_error"
    assert tool_error("denied", "approval_invalid")["code"] == "approval_invalid"


def test_application_mounts_runs_on_the_model_gateway() -> None:
    app = create_app(Settings(app_env="test"))
    paths = set(app.openapi()["paths"])

    assert {
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/cancel",
    } <= paths
    service = app.state.runs
    assert isinstance(service, RunService)
    assert service.engine.gateway is app.state.model_gateway
    assert service.engine.router is app.state.tools.router  # M13 router wired
    assert service.engine.approvals is app.state.tools.approvals


def test_install_runs_accepts_the_real_gateway() -> None:
    settings = Settings(app_env="test")
    runtime = Runtime(
        database=Database(settings.database_url),
        redis=RedisClient(settings.redis_url),
        expected_schema_revision=None,
    )
    gateway = FakeModelGateway()
    service = install_runs(
        FastAPI(),
        settings,
        runtime,
        authenticator=object(),  # type: ignore[arg-type]
        model_gateway=gateway,
    )
    assert service.engine.gateway is gateway


async def test_placeholder_router_has_no_tools() -> None:
    from app.core.ports.tool_router import ToolCallRequest, ToolContext

    router = NoToolRouter()
    context = ToolContext(user_id="u", run_id="r", profile="chat")
    assert await router.list_tools(context) == ()
    outcome = await router.call(context, ToolCallRequest(call_id="c", tool="x.y", arguments={}))
    assert outcome.status == "failed" and outcome.error_code == "tool_not_found"
