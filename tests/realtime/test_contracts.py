import json
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest
from app.modules.realtime.contracts import parse_client_event
from pydantic import ValidationError

ROOT = Path(__file__).parents[2]


def test_all_client_fixtures_parse_with_runtime_models() -> None:
    fixtures = json.loads(
        (ROOT / "contracts/realtime/v1/fixtures/client-events.json").read_text(encoding="utf-8")
    )

    parsed = [parse_client_event(json.dumps(event)) for event in fixtures]

    assert [event.type for event in parsed] == [
        "session.configure",
        "input_audio.start",
        "input_audio.commit",
        "response.cancel",
        "session.close",
        "ping",
    ]


def test_runtime_parser_rejects_unknown_fields_and_naive_timestamp() -> None:
    fixture = json.loads(
        (ROOT / "contracts/realtime/v1/fixtures/client-events.json").read_text(encoding="utf-8")
    )[0]
    fixture["unknown"] = True
    fixture["at"] = "2026-09-10T10:00:00"

    with pytest.raises(ValidationError):
        parse_client_event(json.dumps(fixture))


def server_event_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(
        (ROOT / "contracts/realtime/v1/server-event.schema.json").read_text(encoding="utf-8")
    )
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
