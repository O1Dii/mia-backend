"""M16 public error codes are registered in contracts/mvp/v1/error-codes.json."""

from __future__ import annotations

import json
from pathlib import Path

from app.api.v1.jobs import ERRORS
from app.modules.jobs.messages import PUBLIC_MESSAGES

REGISTRY = Path("contracts/mvp/v1/error-codes.json")
REUSED = {"invalid_cursor", "idempotency_conflict"}


def test_m16_codes_are_registered() -> None:
    registry = {entry["code"]: entry for entry in json.loads(REGISTRY.read_text("utf-8"))}
    for code, (status, message) in ERRORS.items():
        if code in REUSED:
            continue
        entry = registry[code]
        assert entry["owner"] == "M16"
        assert (entry["http_status"], entry["public_message"]) == (status, message)
        assert entry["retryable"] is (code == "notify_rate_limited")
    for code, message in PUBLIC_MESSAGES.items():
        assert registry[code]["public_message"] == message
        assert registry[code]["http_status"] is None
