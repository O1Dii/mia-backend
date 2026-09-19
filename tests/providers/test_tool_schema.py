"""Regression cover for translating registry tool schemas into Gemini's subset.

The W6 integration gate found that every real Gemini turn failed as soon as the
tool registry was wired in. The provider suites had missed it because their
fixtures declared `parameters={"type": "object"}`, a schema that happens to
contain nothing Gemini rejects. These tests use the *real* registry declaration
instead, so the same class of defect cannot pass again.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from app.modules.tools.registry import current_datetime_registry
from app.providers.ai.base import TextMessage, TextRequest, ToolDefinition
from app.providers.ai.gemini_live import GeminiLiveSetup
from app.providers.ai.gemini_live.transport import GoogleGenAILiveTransportFactory
from app.providers.ai.gemini_text import GeminiTextAIProvider, GeminiTextConfig
from app.providers.ai.tool_schema import to_gemini_schema
from google.genai import types  # type: ignore[import-untyped]
from pydantic import SecretStr
from tests.providers.test_gemini_text import StubModels


def _registry_parameters() -> dict[str, object]:
    return dict(current_datetime_registry().declarations()[0].parameters)


def test_registry_schema_really_carries_keys_gemini_rejects() -> None:
    """Guard the premise: if this stops holding, the tests below prove nothing."""
    assert "additionalProperties" in _registry_parameters()


def test_sdk_function_declaration_accepts_the_translated_registry_schema() -> None:
    declaration = current_datetime_registry().declarations()[0]
    function = types.FunctionDeclaration(
        name=declaration.name,
        description=declaration.description,
        parameters=to_gemini_schema(declaration.parameters),
    )
    assert function.parameters is not None
    assert function.parameters.required == ["timezone"]
    assert function.parameters.properties is not None
    assert set(function.parameters.properties) == {"timezone"}


def test_sdk_function_declaration_rejects_the_untranslated_registry_schema() -> None:
    """The pre-fix mapping passed the raw schema straight through and raised here."""
    with pytest.raises(Exception, match="additionalProperties"):
        types.FunctionDeclaration(name="t", description="d", parameters=_registry_parameters())


def test_live_setup_frame_carries_no_unsupported_schema_keys() -> None:
    setup = GeminiLiveSetup(
        model="configured-live-model",
        locale="ru-BY",
        system_instruction="Respond in Belarusian or Russian.",
        max_output_tokens=512,
        input_transcription=True,
        output_transcription=True,
        context_window_trigger_tokens=25_000,
        context_window_target_tokens=8_000,
        resume_handle="opaque-test-handle",
        tools=current_datetime_registry().declarations(),
    )
    frame = json.dumps(GoogleGenAILiveTransportFactory._wire_setup(setup))
    for rejected in ("additionalProperties", "$defs", "$ref"):
        assert rejected not in frame
    assert "system.get_current_datetime" in frame


def test_translation_drops_unsupported_keywords_and_keeps_supported_ones() -> None:
    converted = to_gemini_schema(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "additionalProperties": False,
            "title": "Args",
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "additionalProperties": False},
                "tags": {"type": "array", "items": {"type": "string", "title": "Tag"}},
            },
        }
    )
    assert converted == {
        "title": "Args",
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "tags": {"type": "array", "items": {"type": "string", "title": "Tag"}},
        },
    }


def test_translation_inlines_nested_definitions() -> None:
    converted = to_gemini_schema(
        {
            "type": "object",
            "properties": {"where": {"$ref": "#/$defs/Place", "description": "override"}},
            "$defs": {
                "Place": {
                    "type": "object",
                    "title": "Place",
                    "additionalProperties": False,
                    "properties": {"city": {"type": "string"}},
                }
            },
        }
    )
    assert converted["properties"]["where"] == {
        "type": "object",
        "title": "Place",
        "description": "override",
        "properties": {"city": {"type": "string"}},
    }


def test_translation_expresses_const_as_a_single_value_enum() -> None:
    assert to_gemini_schema({"type": "string", "const": "utc"})["enum"] == ["utc"]


def test_translation_survives_a_definition_that_cannot_be_resolved() -> None:
    converted = to_gemini_schema({"type": "object", "properties": {"x": {"$ref": "#/$defs/Gone"}}})
    assert converted["properties"]["x"] == {}


def test_every_registered_tool_translates_into_an_sdk_declaration() -> None:
    for declaration in current_datetime_registry().declarations():
        assert isinstance(declaration, ToolDefinition)
        types.FunctionDeclaration(
            name=declaration.name,
            description=declaration.description,
            parameters=to_gemini_schema(declaration.parameters),
        )


async def test_text_provider_streams_with_the_real_registry_tool_attached() -> None:
    """The exact path that failed in W6: every live turn raised before a request went out."""
    models = StubModels()
    provider = GeminiTextAIProvider(
        GeminiTextConfig(api_key=SecretStr("unused-in-this-test"), model="configured-text-model"),
        models=models,
    )
    request = TextRequest(
        turn_id=UUID("40000000-0000-4000-8000-000000000009"),
        response_id=UUID("50000000-0000-4000-8000-000000000009"),
        system_instruction="Answer briefly.",
        messages=(TextMessage(role="user", text="Который час?"),),
        tools=current_datetime_registry().declarations(),
    )

    events = [event async for event in provider.stream(request)]

    assert not any(type(event).__name__ == "ProviderError" for event in events)
    assert len(models.calls) == 1
    config = models.calls[0][2]
    assert config.tools is not None
    declared = config.tools[0].function_declarations[0]
    assert declared.name == "system.get_current_datetime"
