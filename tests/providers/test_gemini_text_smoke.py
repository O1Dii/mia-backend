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
from uuid import uuid4

import pytest
from app.modules.tools.registry import current_datetime_registry
from app.providers.ai.base import ProviderError, TextMessage, TextRequest, ToolFeedback
from app.providers.ai.gemini_text import GeminiTextAIProvider, GeminiTextConfig
from pydantic import SecretStr


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
    assert not [event for event in events if isinstance(event, ProviderError)]


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
    errors = [event for event in events if isinstance(event, ProviderError)]
    assert not errors, (
        f"{model} rejected the tool continuation; if this is a Gemini 3.x model it "
        "requires thought_signature passthrough (see the S13-R1 handoff)"
    )
