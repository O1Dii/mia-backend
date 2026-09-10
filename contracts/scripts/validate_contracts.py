#!/usr/bin/env python3
"""Validate all S00 contract artifacts without reading repository secrets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate_spec


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def event_types(schema: dict[str, Any]) -> set[str]:
    return {
        branch["allOf"][1]["properties"]["type"]["const"]
        for branch in schema["oneOf"]
    }


def validate_event_fixture(schema_path: Path, fixture_path: Path) -> int:
    schema = load_json(schema_path)
    events = load_json(fixture_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    if not isinstance(events, list):
        raise AssertionError(f"{fixture_path}: fixture root must be an array")

    for index, event in enumerate(events):
        errors = sorted(validator.iter_errors(event), key=lambda error: list(error.path))
        if errors:
            details = "; ".join(error.message for error in errors)
            raise AssertionError(f"{fixture_path}[{index}]: {details}")

    fixture_types = [event["type"] for event in events]
    expected_types = event_types(schema)
    if set(fixture_types) != expected_types or len(fixture_types) != len(expected_types):
        raise AssertionError(
            f"{fixture_path}: expected exactly one fixture for each of "
            f"{sorted(expected_types)}, got {fixture_types}"
        )

    sequences = [event["seq"] for event in events]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise AssertionError(f"{fixture_path}: seq values must be unique and increasing")

    return len(events)


def validate_schema_documents() -> int:
    schema_paths = sorted(CONTRACTS.rglob("*.schema.json"))
    for path in schema_paths:
        Draft202012Validator.check_schema(load_json(path))
    return len(schema_paths)


def validate_errors(openapi: dict[str, Any], realtime: dict[str, Any]) -> int:
    registry_path = CONTRACTS / "errors/v1/error-codes.json"
    registry = load_json(registry_path)
    registry_schema = load_json(CONTRACTS / "errors/v1/error-codes.schema.json")
    Draft202012Validator(
        registry_schema, format_checker=FormatChecker()
    ).validate(registry)

    codes = [record["code"] for record in registry]
    if len(codes) != len(set(codes)):
        raise AssertionError("error-codes.json: public codes must be unique")

    expected = set(codes)
    embedded_sets = {
        "error.schema.json": set(
            load_json(CONTRACTS / "errors/v1/error.schema.json")["properties"]
            ["error"]["properties"]["code"]["enum"]
        ),
        "openapi.json": set(
            openapi["components"]["schemas"]["ErrorCode"]["enum"]
        ),
        "server-event.schema.json": set(realtime["$defs"]["errorCode"]["enum"]),
    }
    for source, embedded in embedded_sets.items():
        if embedded != expected:
            raise AssertionError(f"{source}: public error codes differ from registry")

    error_validator = Draft202012Validator(
        load_json(CONTRACTS / "errors/v1/error.schema.json"),
        format_checker=FormatChecker(),
    )
    error_validator.validate(load_json(CONTRACTS / "errors/v1/error.example.json"))
    return len(codes)


def validate_openapi_governance(openapi: dict[str, Any]) -> int:
    validate_spec(openapi)
    operation_count = 0
    for path, path_item in openapi["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_count += 1
            if path.startswith("/v1/") and method != "get":
                parameters = operation.get("parameters", [])
                has_idempotency = any(
                    parameter.get("$ref") == "#/components/parameters/IdempotencyKey"
                    for parameter in parameters
                )
                if not has_idempotency:
                    raise AssertionError(
                        f"{method.upper()} {path}: Idempotency-Key is required"
                    )
            for status, response in operation["responses"].items():
                if "$ref" in response:
                    continue
                headers = response.get("headers", {})
                if "X-Request-ID" not in headers:
                    raise AssertionError(
                        f"{method.upper()} {path} {status}: X-Request-ID is required"
                    )
    return operation_count


def main() -> None:
    schema_count = validate_schema_documents()
    openapi = load_json(CONTRACTS / "openapi/v1/openapi.json")
    operation_count = validate_openapi_governance(openapi)
    realtime = load_json(CONTRACTS / "realtime/v1/server-event.schema.json")
    error_count = validate_errors(openapi, realtime)

    event_count = 0
    event_count += validate_event_fixture(
        CONTRACTS / "realtime/v1/client-event.schema.json",
        CONTRACTS / "realtime/v1/fixtures/client-events.json",
    )
    event_count += validate_event_fixture(
        CONTRACTS / "realtime/v1/server-event.schema.json",
        CONTRACTS / "realtime/v1/fixtures/server-events.json",
    )
    event_count += validate_event_fixture(
        CONTRACTS / "ai/v1/ai-event.schema.json",
        CONTRACTS / "ai/v1/fixtures/ai-events.json",
    )

    print(
        "PASS: "
        f"{schema_count} JSON Schemas, {operation_count} OpenAPI operations, "
        f"{event_count} event fixtures, {error_count} public error codes"
    )


if __name__ == "__main__":
    main()
