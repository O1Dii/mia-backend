from datetime import UTC, datetime
from uuid import UUID

import pytest
from app.modules.orchestration import (
    ContextBudgetExceeded,
    ContextBuilder,
    ContextEntry,
    TextOrchestrationService,
    UserProfile,
)
from app.providers.ai.fake import FakeAIProvider, FakeTextResponse


class CharacterEstimator:
    def estimate(self, text: str) -> int:
        return len(text)


def test_context_builder_keeps_a_contiguous_recent_suffix_within_budget() -> None:
    builder = ContextBuilder(
        max_input_tokens=120, message_overhead_tokens=1, estimator=CharacterEstimator()
    )
    built = builder.build(
        stable_instruction="Be useful.",
        profile=UserProfile(locale="ru-BY", timezone="Europe/Minsk"),
        summary="Earlier context.",
        entries=(
            ContextEntry(role="user", text="oldest message"),
            ContextEntry(role="assistant", text="middle"),
            ContextEntry(role="user", text="latest"),
        ),
    )

    assert built.estimated_input_tokens <= 120
    assert [message.text for message in built.messages] == ["middle", "latest"]
    assert built.dropped_entries == 1
    assert "ru-BY" in built.system_instruction
    assert "Europe/Minsk" in built.system_instruction


def test_context_builder_fails_when_stable_context_consumes_budget() -> None:
    builder = ContextBuilder(max_input_tokens=3, estimator=CharacterEstimator())

    with pytest.raises(ContextBudgetExceeded, match="stable context"):
        builder.build(
            stable_instruction="long",
            profile=UserProfile(locale="ru", timezone="UTC"),
            entries=(ContextEntry(role="user", text="hello"),),
        )


@pytest.mark.asyncio
async def test_orchestration_streams_provider_events_without_executing_tools() -> None:
    provider = FakeAIProvider(
        FakeTextResponse(text_chunks=("Привет!",)),
        clock=lambda: datetime(2026, 9, 10, tzinfo=UTC),
    )
    service = TextOrchestrationService(
        provider,
        ContextBuilder(max_input_tokens=512),
    )

    events = [
        event
        async for event in service.stream_turn(
            turn_id=UUID("40000000-0000-4000-8000-000000000001"),
            response_id=UUID("50000000-0000-4000-8000-000000000001"),
            stable_instruction="Be useful.",
            entries=(ContextEntry(role="user", text="Привет"),),
            profile=UserProfile(locale="ru-BY", timezone="Europe/Minsk"),
        )
    ]

    assert [event.event_type for event in events] == [
        "response.started",
        "text.delta",
        "response.completed",
        "usage.reported",
    ]
