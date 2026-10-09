"""mvp/v1 JSON Schemas: examples pass, contract violations fail."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest
from app.core.ports import canonical_args_hash

from tests.fakes.contracts import MVP, mvp_errors, validator_module
from tests.fakes.contracts import load_mvp_example as load_example

Check = Callable[[str, Any, str | None], bool]


@pytest.fixture(scope="module")
def contracts() -> ModuleType:
    return validator_module()


@pytest.fixture(scope="module")
def is_valid() -> Check:
    def check(stem: str, document: Any, definition: str | None = None) -> bool:
        return not mvp_errors(document, stem, definition)

    return check


def test_mvp_validation_passes(contracts: ModuleType) -> None:
    examples, events, operations = contracts.validate_mvp()

    assert examples >= 20
    assert events == 14
    assert operations >= 40


@pytest.mark.parametrize("name", ["timer", "linear", "mindweaver", "focus"])
def test_plugin_examples_are_valid(contracts: ModuleType, is_valid: Check, name: str) -> None:
    manifest = load_example(f"plugin-manifest/{name}.mia.json")

    assert is_valid("plugin-manifest", manifest, None)
    contracts.check_manifest_semantics(manifest, name)


def test_plugin_examples_cover_each_server_kind() -> None:
    kinds = {
        load_example(f"plugin-manifest/{name}.mia.json")["server"]["kind"]
        for name in ("timer", "linear", "mindweaver", "focus")
    }

    assert kinds == {"builtin", "remote", "declarative"}


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda m: m["server"].update(kind="worker"), id="worker-not-in-mvp"),
        pytest.param(lambda m: m["permissions"].update(network=[]), id="remote-needs-network"),
        pytest.param(lambda m: m["tools"]["list_issues"].update(risk="admin"), id="unknown-risk"),
        pytest.param(lambda m: m.update(schemaVersion="2.0"), id="old-schema-version"),
        pytest.param(lambda m: m.update(extra=True), id="unknown-field"),
        pytest.param(lambda m: m["server"].update(url="http://mcp.linear.app"), id="plain-http"),
    ],
)
def test_invalid_manifests_are_rejected(
    is_valid: Check, mutate: Callable[[dict[str, Any]], None]
) -> None:
    manifest = load_example("plugin-manifest/linear.mia.json")
    mutate(manifest)

    assert not is_valid("plugin-manifest", manifest, None)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m["permissions"].update(network=["evil.example.com"]), "permissions.network"),
        (
            lambda m: m["surfaces"]["tiles"][0]["refresh"].update(tool="delete_issue"),
            "read tool",
        ),
        (
            lambda m: m["surfaces"]["tiles"][0].update(resourceUri="ui://other/x"),
            "resourceUri",
        ),
    ],
)
def test_manifest_cross_field_rules(
    contracts: ModuleType, mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    manifest = load_example("plugin-manifest/linear.mia.json")
    mutate(manifest)

    with pytest.raises(AssertionError, match=message):
        contracts.check_manifest_semantics(manifest, "linear")


@pytest.mark.parametrize(
    "speech",
    [
        "Смотрите **здесь**",
        "Ссылка https://example.com",
        "Первая строка\nвторая",
        "а" * 301,
        "",
    ],
)
def test_speech_must_be_short_plain_text(is_valid: Check, speech: str) -> None:
    response = load_example("assistant-response/card.json")
    response["speech"] = speech

    assert not is_valid("assistant-response", response, None)


def test_approval_status_and_decision_must_agree(is_valid: Check) -> None:
    approved = load_example("approval/approved.json")
    without_decision = {**approved, "decision": None}
    pending = load_example("approval/pending.json")
    pending_with_decision = {**pending, "decision": approved["decision"]}

    assert is_valid("approval", approved, None)
    assert not is_valid("approval", without_decision, None)
    assert not is_valid("approval", pending_with_decision, None)


def test_args_hash_is_canonical_and_shared(contracts: ModuleType) -> None:
    pending = load_example("approval/pending.json")
    reordered = dict(reversed(list(pending["arguments"].items())))

    assert canonical_args_hash(reordered) == pending["args_hash"]
    assert contracts.canonical_args_hash(pending["arguments"]) == pending["args_hash"]
    assert canonical_args_hash({"text": "молоко"}) != canonical_args_hash({"text": "Молоко"})


def test_failed_device_result_needs_error(is_valid: Check) -> None:
    result: dict[str, Any] = {"call_id": "c1", "status": "failed"}

    assert not is_valid("device-tools", result, "DeviceToolResult")
    result["error"] = {"code": "device_locked", "message": "Locked", "retryable": True}
    assert is_valid("device-tools", result, "DeviceToolResult")


def run_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = json.loads(
        (MVP / "fixtures/run-events.json").read_text(encoding="utf-8")
    )
    return events


def test_run_event_envelope_is_strict(is_valid: Check) -> None:
    event = copy.deepcopy(run_events()[0])
    unknown_type = {**event, "type": "run.paused"}
    extra_field = {**event, "debug": True}
    zero_sequence = {**event, "sequence": 0}
    wrong_payload = {**event, "payload": {"text": "hi"}}

    assert is_valid("run-events", event, None)
    for broken in (unknown_type, extra_field, zero_sequence, wrong_payload):
        assert not is_valid("run-events", broken, None)


def test_naive_timestamps_are_rejected(is_valid: Check) -> None:
    fact = load_example("memory/fact.json")
    fact["created_at"] = "2026-10-09T10:00:00"

    assert not is_valid("memory", fact, None)


def test_memory_records_are_discriminated_by_kind(is_valid: Check) -> None:
    person = load_example("memory/person.json")

    assert is_valid("memory", person, None)
    assert not is_valid("memory", {**person, "kind": "fact"}, None)
    assert not is_valid("memory", {**person, "identifiers": {"phone": "89001234567"}}, None)


def test_schedule_requires_rrule_and_iana_zone(is_valid: Check) -> None:
    schedule = load_example("job/Schedule/daily-digest.json")

    assert is_valid("job", schedule, "Schedule")
    assert not is_valid("job", {**schedule, "rule": "every day at 8"}, "Schedule")
    assert not is_valid("job", {**schedule, "timezone": "+03:00"}, "Schedule")


def test_openapi_rules_reject_missing_idempotency(contracts: ModuleType) -> None:
    openapi = contracts.load_mvp_openapi()
    operation = openapi["paths"]["/v1/jobs"]["post"]
    operation["parameters"] = []

    with pytest.raises(AssertionError, match="Idempotency-Key"):
        contracts.check_openapi_rules(openapi)


def test_openapi_only_exempts_openai_compatible_endpoint(contracts: ModuleType) -> None:
    openapi = contracts.load_mvp_openapi()
    exempt = [
        (path, method)
        for path, item in openapi["paths"].items()
        for method, operation in item.items()
        if isinstance(operation, dict) and operation.get("x-mia-idempotency") == "exempt"
    ]

    # Both OpenAI-compatible endpoints (ADR-0013; /v1/responses added by M50): SDKs send no key.
    assert exempt == [("/v1/chat/completions", "post"), ("/v1/responses", "post")]
