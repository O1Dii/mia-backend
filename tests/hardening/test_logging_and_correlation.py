"""Structured logging redaction, correlation IDs and operational metrics."""

import json
import logging
from typing import Any
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.core.correlation import CorrelationMiddleware, current_request_id, resolve_request_id
from app.core.logging import JsonFormatter, configure_logging
from app.core.metrics import METRICS, MetricsRegistry
from app.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "synthetic-secret-and-transcript"


def formatted(record: logging.LogRecord) -> dict[str, Any]:
    payload = json.loads(JsonFormatter().format(record))
    assert isinstance(payload, dict)
    return payload


def make_record(message: str, **extra: Any) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_unknown_events_arguments_and_extra_fields_are_dropped() -> None:
    record = logging.LogRecord("mia", logging.ERROR, __file__, 1, "user said %s", (SECRET,), None)
    record.exc_text = SECRET
    record.path = f"/v1/conversations?token={SECRET}"
    record.transcript = SECRET
    record.reason = SECRET
    record.duration_ms = 12
    payload = formatted(record)
    assert SECRET not in json.dumps(payload)
    assert payload["event"] == "redacted_event"
    assert payload["logger"] == "external"
    assert payload["duration_ms"] == 12
    assert {"path", "transcript", "reason", "message", "msg"} & set(payload) == set()


def test_exception_records_expose_only_the_exception_class() -> None:
    try:
        raise ValueError(SECRET)
    except ValueError:
        record = logging.LogRecord("app.x", logging.ERROR, __file__, 1, "boom", (), None)
        import sys

        record.exc_info = sys.exc_info()
    payload = formatted(record)
    assert SECRET not in json.dumps(payload)
    assert payload["error_type"] == "ValueError"


def test_free_form_request_ids_are_hashed_and_uuid_ids_are_kept() -> None:
    hashed = formatted(make_record("request_completed", request_id=SECRET))
    assert SECRET not in json.dumps(hashed)
    assert "request_id" not in hashed
    assert len(hashed["request_id_hash"]) == 16

    server_id = uuid4().hex
    kept = formatted(make_record("request_completed", request_id=server_id))
    assert kept["request_id"] == server_id
    assert kept["event"] == "request_completed"


def test_only_typed_allow_listed_fields_are_serialized() -> None:
    conversation_id = uuid4()
    payload = formatted(
        make_record(
            "admission_rejected",
            reason="daily_text_turns",
            conversation_id=conversation_id,
            status_code="200",
            method="TRACE",
            route="/v1/conversations/{conversation_id}/messages:stream",
        )
    )
    assert payload["reason"] == "daily_text_turns"
    assert payload["conversation_id"] == str(conversation_id)
    assert payload["route"] == "/v1/conversations/{conversation_id}/messages:stream"
    assert "status_code" not in payload
    assert "method" not in payload


def test_configure_logging_redacts_server_logs_and_disables_access_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    try:
        logging.getLogger("uvicorn.error").error("connection failed for %s", SECRET)
        logging.getLogger("uvicorn.access").info("GET /v1/conversations?token=%s", SECRET)
        captured = capsys.readouterr().err
        assert SECRET not in captured
        assert json.loads(captured.strip())["event"] == "redacted_event"
    finally:
        logging.getLogger("uvicorn.access").disabled = False
        logging.basicConfig(force=True)


def test_request_ids_are_echoed_generated_and_logged(
    caplog: pytest.LogCaptureFixture, client: TestClient
) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.correlation"):
        supplied = client.get("/health/live", headers={"X-Request-ID": "abc-123_XYZ"})
        rejected = client.get("/health/live", headers={"X-Request-ID": "not a valid id\n"})
    assert supplied.headers["X-Request-ID"] == "abc-123_XYZ"
    assert rejected.headers["X-Request-ID"] not in {"not a valid id\n", ""}
    assert len(rejected.headers["X-Request-ID"]) == 32
    completed = [record for record in caplog.records if record.msg == "request_completed"]
    assert [record.request_id for record in completed] == [  # type: ignore[attr-defined]
        "abc-123_XYZ",
        rejected.headers["X-Request-ID"],
    ]
    assert [record.route for record in completed] == ["/health/live"] * 2  # type: ignore[attr-defined]
    assert [record.status_code for record in completed] == [200, 200]  # type: ignore[attr-defined]


def test_correlation_id_reaches_module_loggers_without_explicit_extra() -> None:
    seen: list[str | None] = []
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware, metrics=MetricsRegistry())

    @app.get("/probe")
    async def probe() -> dict[str, str]:
        seen.append(current_request_id())
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/probe", headers={"X-Request-ID": "probe-request-id"})
    assert seen == ["probe-request-id"]
    assert response.headers["X-Request-ID"] == "probe-request-id"


def test_unhandled_endpoint_failures_return_the_error_envelope() -> None:
    app = create_app(Settings(app_env="test"), install_modules=False)

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError(SECRET)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"X-Request-ID": "boom-request-id"})
    assert response.status_code == 500
    assert SECRET not in response.text
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": "An unexpected error occurred.",
        "request_id": "boom-request-id",
        "retryable": False,
    }


def test_resolve_request_id_rejects_oversized_and_unsafe_values() -> None:
    assert resolve_request_id("valid_id-1") == "valid_id-1"
    assert len(resolve_request_id("x" * 65)) == 32
    assert len(resolve_request_id("../../etc/passwd")) == 32
    assert len(resolve_request_id(None)) == 32


def test_metrics_endpoint_is_opt_in_and_content_free() -> None:
    disabled = create_app(Settings(app_env="test"), install_modules=False)
    with TestClient(disabled) as client:
        assert client.get("/metrics").status_code == 404

    enabled = create_app(Settings(app_env="test", metrics_enabled=True), install_modules=False)
    before = METRICS.value(
        "mia_http_requests_total", {"method": "GET", "route": "/health/live", "status": "2xx"}
    )
    with TestClient(enabled) as client:
        client.get("/health/live?token=secret-query-value")
        body = client.get("/metrics").text
    after = METRICS.value(
        "mia_http_requests_total", {"method": "GET", "route": "/health/live", "status": "2xx"}
    )
    assert after == before + 1
    assert "secret-query-value" not in body
    assert 'mia_http_requests_total{method="GET",route="/health/live",status="2xx"}' in body
    assert "mia_http_request_duration_seconds_count" in body
    assert "/metrics" not in body.replace('route="/metrics"', "")


def test_metrics_registry_rejects_unsafe_labels_and_counts_exactly() -> None:
    registry = MetricsRegistry()
    registry.inc("mia_test_total", {"reason": "daily_text_turns"}, 3)
    registry.inc("mia_test_total", {"reason": "daily_text_turns"})
    registry.observe("mia_test_seconds", 0.2, {"route": "/health/live"})
    rendered = registry.render()
    assert 'mia_test_total{reason="daily_text_turns"} 4' in rendered
    assert 'mia_test_seconds_bucket{route="/health/live",le="0.25"} 1' in rendered
    assert 'mia_test_seconds_count{route="/health/live"} 1' in rendered
    with pytest.raises(ValueError):
        registry.inc("mia_test_total", {"reason": 'user said "hello"'})
    with pytest.raises(ValueError):
        registry.inc("http_requests_total")
