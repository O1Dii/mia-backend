"""Credential-gated smoke for the manual tool loop against real Gemini.

The deterministic suites drive a stub `models` object, so they cannot see what
the API does with the continuation turn MIA builds after a tool runs. W6 found
that the whole Gemini 3.x family rejects it: reconstructing the `functionCall`
part from the provider-neutral `ToolFeedback` drops the `thought_signature` the
model emitted, and Gemini 3.x requires that signature to be echoed back.

This smoke skips without credentials, exactly like the Live one.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from uuid import uuid4

import pytest
from app.modules.tools.registry import current_datetime_registry
from app.providers.ai.base import (
    AIEvent,
    ProviderError,
    TextMessage,
    TextRequest,
    ToolFeedback,
)
from app.providers.ai.gemini_text import GeminiTextAIProvider, GeminiTextConfig
from pydantic import SecretStr

# Throttling and transport trouble say nothing about whether MIA builds a request
# the API accepts, which is the only thing these smokes exist to judge. Treating
# them as failures would make the gate cry wolf; treating them as passes would
# hide a real rejection. They skip instead, loudly.
_ENVIRONMENTAL_CODES = frozenset(
    {"provider_rate_limited", "provider_timeout", "provider_unavailable"}
)


def _assert_accepted(events: Sequence[AIEvent], *, model: str, what: str) -> None:
    errors = [event for event in events if isinstance(event, ProviderError)]
    if not errors:
        return
    codes = {error.code for error in errors}
    if codes <= _ENVIRONMENTAL_CODES:
        pytest.skip(f"{model} could not be reached to judge {what}: {sorted(codes)}")
    raise AssertionError(
        f"{model} rejected {what} with {sorted(codes)}. A 3.x text model rejects the "
        "continuation turn because MIA sends no thought_signature; see the S13-R1 handoff."
    )


def _provider(model: str) -> GeminiTextAIProvider:
    return GeminiTextAIProvider(
        GeminiTextConfig(
            api_key=SecretStr(os.environ["GEMINI_API_KEY"]),
            model=model,
            request_timeout_seconds=30.0,
        )
    )


async def test_real_tool_declaration_is_accepted_when_credentials_are_configured() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_TEXT_MODEL")
    if not api_key or not model:
        pytest.skip("GEMINI_API_KEY and GEMINI_TEXT_MODEL are not configured in the process")

    request = TextRequest(
        turn_id=uuid4(),
        response_id=uuid4(),
        system_instruction="You are MIA. Answer briefly.",
        messages=(TextMessage(role="user", text="Reply with one short sentence."),),
        tools=current_datetime_registry().declarations(),
    )
    events = [event async for event in _provider(model).stream(request)]
    _assert_accepted(events, model=model, what="the tool declaration")


async def test_real_tool_continuation_is_accepted_when_credentials_are_configured() -> None:
    """The turn that carries a finished tool result back to the model.

    This is the one the manual loop depends on, and the one Gemini 3.x rejects.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_TEXT_MODEL")
    if not api_key or not model:
        pytest.skip("GEMINI_API_KEY and GEMINI_TEXT_MODEL are not configured in the process")

    request = TextRequest(
        turn_id=uuid4(),
        response_id=uuid4(),
        system_instruction="You are MIA. Answer briefly.",
        messages=(TextMessage(role="user", text="What is the current time in Europe/Minsk?"),),
        tools=current_datetime_registry().declarations(),
        tool_feedback=(
            ToolFeedback(
                name="system.get_current_datetime",
                arguments={"timezone": "Europe/Minsk"},
                response={
                    "timezone": "Europe/Minsk",
                    "local_datetime": "2026-09-20T01:26:11.443630+03:00",
                },
            ),
        ),
    )
    events = [event async for event in _provider(model).stream(request)]
    _assert_accepted(events, model=model, what="the tool continuation")
