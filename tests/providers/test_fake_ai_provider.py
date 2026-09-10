import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import jsonschema  # type: ignore[import-untyped]
import pytest
from app.providers.ai.base import TextMessage, TextRequest, ToolCallRequested, ToolDefinition
from app.providers.ai.fake import FakeAIProvider, FakeTextResponse, FakeToolCall

TURN_ID = UUID("40000000-0000-4000-8000-000000000001")
RESPONSE_ID = UUID("50000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)


def request(*, tools: tuple[ToolDefinition, ...] = ()) -> TextRequest:
    return TextRequest(
        turn_id=TURN_ID,
        response_id=RESPONSE_ID,
        messages=(TextMessage(role="user", text="Который час?"),),
        system_instruction="Be concise.",
        tools=tools,
    )


@pytest.mark.asyncio
async def test_fake_stream_is_deterministic_and_contract_valid() -> None:
    response = FakeTextResponse(text_chunks=("Сейчас ", "13:00."), input_tokens=12, output_tokens=8)
    first = [event async for event in FakeAIProvider(response, clock=lambda: NOW).stream(request())]
    second = [
        event async for event in FakeAIProvider(response, clock=lambda: NOW).stream(request())
    ]

    assert first == second
    assert [event.event_type for event in first] == [
        "response.started",
        "text.delta",
        "text.delta",
        "response.completed",
        "usage.reported",
    ]
    assert [event.seq for event in first] == [1, 2, 3, 4, 5]

    schema_path = Path(__file__).parents[2] / "contracts/ai/v1/ai-event.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    contract_validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    for event in first:
        contract_validator.validate(event.to_contract())


@pytest.mark.asyncio
async def test_fake_tool_call_uses_declared_policy_but_does_not_execute() -> None:
    calls = FakeToolCall(name="system.get_current_datetime", arguments={"timezone": "Europe/Minsk"})
    tool = ToolDefinition(
        name="system.get_current_datetime",
        description="Return the current local time.",
        parameters={"type": "object"},
        risk="read",
    )
    events = [
        event
        async for event in FakeAIProvider(
            FakeTextResponse(text_chunks=(), tool_call=calls), clock=lambda: NOW
        ).stream(request(tools=(tool,)))
    ]

    tool_event = next(event for event in events if isinstance(event, ToolCallRequested))
    assert tool_event.arguments == {"timezone": "Europe/Minsk"}
    assert tool_event.risk == "read"
    assert events[-2].payload() == {"finish_reason": "tool_result"}


@pytest.mark.asyncio
async def test_fake_rejects_an_undeclared_scripted_tool() -> None:
    provider = FakeAIProvider(
        FakeTextResponse(
            text_chunks=(),
            tool_call=FakeToolCall(name="system.unknown", arguments={}),
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="undeclared tool"):
        _ = [event async for event in provider.stream(request())]
