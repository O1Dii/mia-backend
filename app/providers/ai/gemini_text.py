"""Google Gen AI SDK adapter for provider-neutral text streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID, uuid5

from google import genai
from google.genai import types  # type: ignore[import-untyped]
from pydantic import SecretStr

from app.providers.ai.base import (
    AIEvent,
    ProviderError,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    TextRequest,
    ToolCallRequested,
    UsageReported,
)
from app.providers.ai.base.events import FinishReason, ProviderErrorCode
from app.providers.ai.tool_schema import to_gemini_schema


class _GeminiModels(Protocol):
    def generate_content_stream(
        self,
        *,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> Awaitable[AsyncIterator[types.GenerateContentResponse]]: ...


@dataclass(frozen=True, slots=True)
class GeminiTextConfig:
    """Adapter-owned provider settings; credential repr remains redacted."""

    api_key: SecretStr
    model: str
    model_alias: str = "text-default"
    request_timeout_seconds: float = 30.0
    temperature: float = 0.2
    max_output_tokens: int = 1024

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("Gemini text model must be configured")
        if self.request_timeout_seconds <= 0:
            raise ValueError("Gemini request timeout must be positive")
        if self.max_output_tokens < 1:
            raise ValueError("Gemini max output tokens must be positive")


class _InvalidProviderResponse(Exception):
    pass


class GeminiTextAIProvider:
    """Map SDK chunks to stable AIEvent values and keep manual tool control."""

    def __init__(
        self,
        config: GeminiTextConfig,
        *,
        models: _GeminiModels | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        if models is None:
            client = genai.Client(api_key=config.api_key.get_secret_value())
            models = cast(_GeminiModels, client.aio.models)
        self._models = models
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _event_id(response_id: UUID, seq: int) -> UUID:
        return uuid5(response_id, f"gemini-text:{seq}")

    @staticmethod
    def _contents(request: TextRequest) -> list[types.Content]:
        contents = [
            types.Content(
                role="model" if message.role == "assistant" else "user",
                parts=[types.Part(text=message.text)],
            )
            for message in request.messages
        ]
        for feedback in request.tool_feedback:
            contents.append(
                types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name=feedback.name, args=dict(feedback.arguments)
                        )
                    ],
                )
            )
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name=feedback.name, response=dict(feedback.response)
                        )
                    ],
                )
            )
        return contents

    def _generation_config(self, request: TextRequest) -> types.GenerateContentConfig:
        declarations = [
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=to_gemini_schema(tool.parameters),
            )
            for tool in request.tools
        ]
        tools = [types.Tool(function_declarations=declarations)] if declarations else None
        return types.GenerateContentConfig(
            system_instruction=request.system_instruction,
            temperature=self._config.temperature,
            max_output_tokens=min(request.max_output_tokens, self._config.max_output_tokens),
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    @staticmethod
    def _finish_reason(raw_reason: object, *, requested_tool: bool) -> FinishReason:
        if requested_tool:
            return "tool_result"
        name = getattr(raw_reason, "value", raw_reason)
        if name in (None, "STOP"):
            return "stop"
        if name == "MAX_TOKENS":
            return "max_output"
        if name in {
            "SAFETY",
            "RECITATION",
            "BLOCKLIST",
            "PROHIBITED_CONTENT",
            "SPII",
            "IMAGE_SAFETY",
        }:
            return "content_filter"
        raise _InvalidProviderResponse("unsupported Gemini finish reason")

    @staticmethod
    def _error_details(error: Exception) -> tuple[ProviderErrorCode, str, bool]:
        if isinstance(error, TimeoutError):
            return "provider_timeout", "Provider request timed out.", True
        if isinstance(error, _InvalidProviderResponse):
            return "provider_invalid_response", "Provider returned an invalid response.", False
        code = getattr(error, "code", None)
        if code == 429:
            return "provider_rate_limited", "Provider rate limit was reached.", True
        if code in {408, 504}:
            return "provider_timeout", "Provider request timed out.", True
        if isinstance(code, int) and code >= 500:
            return "provider_unavailable", "Provider is temporarily unavailable.", True
        if code in {401, 403}:
            return "provider_unavailable", "Provider credentials were rejected.", False
        return "provider_internal_error", "Provider request failed.", False

    async def stream(self, request: TextRequest) -> AsyncIterator[AIEvent]:
        seq = 0
        last_usage: types.GenerateContentResponseUsageMetadata | None = None
        finish_reason: object = None
        requested_tool = False
        declared_tools = {tool.name: tool for tool in request.tools}

        try:
            async with asyncio.timeout(self._config.request_timeout_seconds):
                chunks = await self._models.generate_content_stream(
                    model=self._config.model,
                    contents=self._contents(request),
                    config=self._generation_config(request),
                )
                seq += 1
                yield ResponseStarted(
                    event_id=self._event_id(request.response_id, seq),
                    turn_id=request.turn_id,
                    response_id=request.response_id,
                    seq=seq,
                    at=self._clock(),
                    modalities=("text",),
                )

                async for chunk in chunks:
                    if chunk.usage_metadata is not None:
                        last_usage = chunk.usage_metadata
                    for candidate in chunk.candidates or ():
                        if candidate.finish_reason is not None:
                            finish_reason = candidate.finish_reason
                        if candidate.content is None:
                            continue
                        for part in candidate.content.parts or ():
                            if part.text:
                                seq += 1
                                yield TextDelta(
                                    event_id=self._event_id(request.response_id, seq),
                                    turn_id=request.turn_id,
                                    response_id=request.response_id,
                                    seq=seq,
                                    at=self._clock(),
                                    text=part.text,
                                )
                            if part.function_call is not None:
                                call = part.function_call
                                name = call.name or ""
                                definition = declared_tools.get(name)
                                if definition is None:
                                    raise _InvalidProviderResponse(
                                        "Gemini requested an undeclared function"
                                    )
                                arguments = call.args
                                if arguments is None:
                                    normalized_arguments: Mapping[str, object] = {}
                                elif isinstance(arguments, Mapping):
                                    normalized_arguments = cast(Mapping[str, object], arguments)
                                else:
                                    raise _InvalidProviderResponse(
                                        "Gemini function arguments were not an object"
                                    )
                                seq += 1
                                raw_call_id = call.id or f"{name}:{seq}"
                                yield ToolCallRequested(
                                    event_id=self._event_id(request.response_id, seq),
                                    turn_id=request.turn_id,
                                    response_id=request.response_id,
                                    seq=seq,
                                    at=self._clock(),
                                    call_id=uuid5(request.response_id, raw_call_id),
                                    name=name,
                                    arguments=normalized_arguments,
                                    risk=definition.risk,
                                    requires_confirmation=definition.requires_confirmation,
                                )
                                requested_tool = True

                seq += 1
                yield ResponseCompleted(
                    event_id=self._event_id(request.response_id, seq),
                    turn_id=request.turn_id,
                    response_id=request.response_id,
                    seq=seq,
                    at=self._clock(),
                    finish_reason=self._finish_reason(finish_reason, requested_tool=requested_tool),
                )
                if last_usage is not None:
                    seq += 1
                    yield UsageReported(
                        event_id=self._event_id(request.response_id, seq),
                        turn_id=request.turn_id,
                        response_id=request.response_id,
                        seq=seq,
                        at=self._clock(),
                        provider_alias="gemini",
                        model_alias=self._config.model_alias,
                        input_tokens=last_usage.prompt_token_count or 0,
                        output_tokens=last_usage.candidates_token_count or 0,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code, message, retryable = self._error_details(error)
            seq += 1
            yield ProviderError(
                event_id=self._event_id(request.response_id, seq),
                turn_id=request.turn_id,
                response_id=request.response_id,
                seq=seq,
                at=self._clock(),
                code=code,
                message=message,
                retryable=retryable,
            )
