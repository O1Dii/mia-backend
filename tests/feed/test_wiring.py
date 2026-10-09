"""``/v1/feed`` is mounted by the application and its error code is registered."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.main import create_app

REGISTRY = Path("contracts/mvp/v1/error-codes.json")


def test_application_mounts_the_feed() -> None:
    app = create_app(Settings(app_env="test"))
    operation = app.openapi()["paths"]["/v1/feed"]["get"]
    assert {parameter["name"] for parameter in operation["parameters"]} == {"before", "limit"}


def test_feed_error_code_is_registered() -> None:
    registry = {entry["code"]: entry for entry in json.loads(REGISTRY.read_text("utf-8"))}
    assert registry["invalid_feed_cursor"] == {
        "code": "invalid_feed_cursor",
        "http_status": 400,
        "retryable": False,
        "public_message": "The feed cursor is invalid.",
        "owner": "M18",
    }


def test_contract_declares_the_feed_operation() -> None:
    from tests.feed.test_feed_api import OPENAPI

    operation = OPENAPI["paths"]["/v1/feed"]["get"]
    assert operation["operationId"] == "listFeed"
    assert {"FeedItem", "FeedPage"} <= set(OPENAPI["components"]["schemas"])
