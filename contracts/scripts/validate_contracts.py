#!/usr/bin/env python3
"""Validate all contract artifacts (S00 v1 and M00 mvp/v1) without reading secrets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml  # PyYAML, installed with openapi-spec-validator (jsonschema-path)
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema_path import SchemaPath
from openapi_spec_validator import validate_spec
from openapi_spec_validator.validation import OpenAPIV31SpecValidator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
MVP = CONTRACTS / "mvp/v1"
MVP_ID_PREFIX = "https://contracts.mia.local/mvp/v1/"
MVP_REQUIRED_PLUGIN_EXAMPLES = ("timer", "linear", "mindweaver")
MVP_REQUIRED_PATHS = (
    "/v1/runs",
    "/v1/runs/{run_id}/events",
    "/v1/runs/{run_id}/cancel",
    "/v1/approvals/{approval_id}/decision",
    "/v1/plugins",
    "/v1/installations",
    "/v1/connections",
    "/v1/dashboard",
    "/v1/memory/facts",
    "/v1/memory/people",
    "/v1/memory/preferences",
    "/v1/memory/context",
    "/v1/jobs",
    "/v1/schedules",
    "/v1/inbox",
    "/v1/devices/push-token",
    "/oauth/authorize",
    "/oauth/token",
    "/v1/chat/completions",
    "/mcp",
)


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
    return check_openapi_rules(openapi)


def check_openapi_rules(openapi: dict[str, Any]) -> int:
    """Idempotency-Key on /v1 mutations and X-Request-ID on inline responses.

    An operation may opt out of Idempotency-Key only with
    ``x-mia-idempotency: exempt`` (OpenAI-compatible endpoints).
    """
    operation_count = 0
    for path, path_item in openapi["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_count += 1
            exempt = operation.get("x-mia-idempotency") == "exempt"
            if path.startswith("/v1/") and method != "get" and not exempt:
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


# --- mvp/v1 (M00) ---------------------------------------------------------


def canonical_args_hash(arguments: dict[str, Any]) -> str:
    """Mirror of app.core.ports.approval_service.canonical_args_hash."""
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mvp_schemas() -> dict[str, dict[str, Any]]:
    """Return mvp/v1 schemas keyed by file stem, checking their $id."""
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(MVP.glob("*.schema.json")):
        schema = load_json(path)
        expected_id = MVP_ID_PREFIX + path.name
        if schema.get("$id") != expected_id:
            raise AssertionError(f"{path}: $id must be {expected_id}")
        schemas[path.name.removesuffix(".schema.json")] = schema
    return schemas


def mvp_registry(schemas: dict[str, dict[str, Any]]) -> Registry:
    resources = [
        (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
        for schema in schemas.values()
    ]
    return Registry().with_resources(resources)


def mvp_validator(
    registry: Registry, stem: str, definition: str | None = None
) -> Draft202012Validator:
    target = MVP_ID_PREFIX + f"{stem}.schema.json"
    if definition is not None:
        target += f"#/$defs/{definition}"
    return Draft202012Validator(
        {"$ref": target}, registry=registry, format_checker=FormatChecker()
    )


def assert_valid(validator: Draft202012Validator, document: Any, source: str) -> None:
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise AssertionError(f"{source}: {details}")


def check_manifest_semantics(manifest: dict[str, Any], source: str) -> None:
    """Cross-field rules of mia.json that JSON Schema cannot express."""
    tools = set(manifest["tools"])
    server = manifest["server"]
    if server["kind"] == "remote":
        host = urlsplit(server["url"]).hostname
        if host not in manifest["permissions"]["network"]:
            raise AssertionError(f"{source}: server host {host} must be in permissions.network")
    if server["kind"] == "declarative":
        actions = {action["name"] for action in server["actions"]}
        if actions != tools:
            raise AssertionError(f"{source}: declarative actions {actions} != tools {tools}")
    for tile in manifest.get("surfaces", {}).get("tiles", []):
        refresh = tile.get("refresh")
        if refresh and refresh["tool"] not in tools:
            raise AssertionError(f"{source}: tile {tile['id']} refreshes unknown tool")
        if refresh and manifest["tools"][refresh["tool"]]["risk"] != "read":
            raise AssertionError(f"{source}: tile {tile['id']} must refresh with a read tool")
        prefix = f"ui://{manifest['id']}/"
        if not tile["resourceUri"].startswith(prefix):
            raise AssertionError(f"{source}: tile resourceUri must start with {prefix}")


def validate_mvp_examples(registry: Registry, schemas: dict[str, dict[str, Any]]) -> int:
    """examples/<stem>/*.json validate against the root schema;
    examples/<stem>/<Def>/*.json against #/$defs/<Def>."""
    count = 0
    covered: set[str] = set()
    for path in sorted((MVP / "examples").rglob("*.json")):
        relative = path.relative_to(MVP / "examples")
        stem = relative.parts[0]
        if stem not in schemas:
            raise AssertionError(f"{path}: no schema {stem}.schema.json")
        definition = relative.parts[1] if len(relative.parts) == 3 else None
        if len(relative.parts) not in (2, 3):
            raise AssertionError(f"{path}: unexpected example nesting")
        if definition is not None and definition not in schemas[stem].get("$defs", {}):
            raise AssertionError(f"{path}: {stem} has no $defs/{definition}")
        document = load_json(path)
        assert_valid(mvp_validator(registry, stem, definition), document, str(path))
        if stem == "plugin-manifest":
            check_manifest_semantics(document, str(path))
        if stem == "approval" and definition is None:
            if document["args_hash"] != canonical_args_hash(document["arguments"]):
                raise AssertionError(f"{path}: args_hash does not match arguments")
        covered.add(stem)
        count += 1

    missing = set(schemas) - covered - {"common", "run-events"}  # run-events: fixtures/
    if missing:
        raise AssertionError(f"mvp/v1 schemas without examples: {sorted(missing)}")
    for name in MVP_REQUIRED_PLUGIN_EXAMPLES:
        if not (MVP / f"examples/plugin-manifest/{name}.mia.json").is_file():
            raise AssertionError(f"missing plugin example {name}.mia.json")
    return count


def run_event_types(schema: dict[str, Any]) -> list[str]:
    return [branch["properties"]["type"]["const"] for branch in schema["oneOf"]]


def validate_mvp_run_events(registry: Registry, schemas: dict[str, dict[str, Any]]) -> int:
    fixture_path = MVP / "fixtures/run-events.json"
    events = load_json(fixture_path)
    if not isinstance(events, list):
        raise AssertionError(f"{fixture_path}: fixture root must be an array")
    validator = mvp_validator(registry, "run-events")
    for index, event in enumerate(events):
        assert_valid(validator, event, f"{fixture_path}[{index}]")

    expected = run_event_types(schemas["run-events"])
    types = [event["type"] for event in events]
    if sorted(types) != sorted(expected):
        raise AssertionError(f"{fixture_path}: need one event per type {expected}, got {types}")
    sequences = [event["sequence"] for event in events]
    if sequences != list(range(1, len(events) + 1)):
        raise AssertionError(f"{fixture_path}: sequence must be 1..N without gaps")
    if len({event["cursor"] for event in events}) != len(events):
        raise AssertionError(f"{fixture_path}: cursors must be unique")
    if len({event["run_id"] for event in events}) != 1:
        raise AssertionError(f"{fixture_path}: all events must belong to one run")
    return len(events)


def load_mvp_openapi() -> dict[str, Any]:
    with (MVP / "openapi.yaml").open(encoding="utf-8") as source:
        document = yaml.safe_load(source)
    if not isinstance(document, dict):
        raise AssertionError("mvp/v1/openapi.yaml: root must be a mapping")
    return document


def _local_mvp_handler(uri: str) -> Any:
    """Resolve mvp/v1 $ids and file:// refs from disk; never touch the network."""
    parts = urlsplit(uri)
    if parts.scheme == "file":
        path = Path(unquote(parts.path)).resolve()
    elif uri.startswith(MVP_ID_PREFIX):
        path = (MVP / parts.path.rsplit("/", 1)[-1]).resolve()
    else:
        raise AssertionError(f"mvp/v1/openapi.yaml: external reference not allowed: {uri}")
    if MVP.resolve() not in path.parents:
        raise AssertionError(f"mvp/v1/openapi.yaml: reference outside mvp/v1: {uri}")
    return load_json(path)


def validate_mvp_openapi() -> int:
    openapi = load_mvp_openapi()
    spec_path = SchemaPath.from_dict(
        openapi,
        base_uri=(MVP / "openapi.yaml").resolve().as_uri(),
        handlers={"file": _local_mvp_handler, "https": _local_mvp_handler},
    )
    OpenAPIV31SpecValidator(spec_path).validate()
    missing = [path for path in MVP_REQUIRED_PATHS if path not in openapi["paths"]]
    if missing:
        raise AssertionError(f"mvp/v1/openapi.yaml: missing paths {missing}")
    return check_openapi_rules(openapi)


def validate_mvp() -> tuple[int, int, int]:
    schemas = mvp_schemas()
    registry = mvp_registry(schemas)
    examples = validate_mvp_examples(registry, schemas)
    events = validate_mvp_run_events(registry, schemas)
    operations = validate_mvp_openapi()
    return examples, events, operations


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

    mvp_examples, mvp_events, mvp_operations = validate_mvp()

    print(
        "PASS: "
        f"{schema_count} JSON Schemas, {operation_count} OpenAPI operations, "
        f"{event_count} event fixtures, {error_count} public error codes; "
        f"mvp/v1: {mvp_examples} examples, {mvp_events} run events, "
        f"{mvp_operations} OpenAPI operations"
    )


if __name__ == "__main__":
    main()
