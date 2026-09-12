"""Thin orchestration boundary that never imports a provider SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from uuid import UUID

from app.modules.orchestration.context import ContextBuilder, ContextEntry, UserProfile
from app.providers.ai.base import AIEvent, TextAIProvider, TextRequest, ToolDefinition


class TextOrchestrationService:
    def __init__(self, provider: TextAIProvider, context_builder: ContextBuilder) -> None:
        self._provider = provider
        self._context_builder = context_builder

    async def stream_turn(
        self,
        *,
        turn_id: UUID,
        response_id: UUID,
        stable_instruction: str,
        entries: Sequence[ContextEntry],
        profile: UserProfile,
        summary: str | None = None,
        tool_results: Sequence[str] = (),
        tools: Sequence[ToolDefinition] = (),
        max_output_tokens: int = 512,
    ) -> AsyncIterator[AIEvent]:
        context = self._context_builder.build(
            stable_instruction=stable_instruction,
            entries=entries,
            profile=profile,
            summary=summary,
            tool_results=tool_results,
        )
        request = TextRequest(
            turn_id=turn_id,
            response_id=response_id,
            messages=context.messages,
            system_instruction=context.system_instruction,
            tools=tools,
            max_output_tokens=max_output_tokens,
        )
        events = self._provider.stream(request)
        try:
            async for event in events:
                yield event
        finally:
            close = getattr(events, "aclose", None)
            if close is not None:
                await close()
