"""The application mounts the device tools endpoints and registers their error code."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.main import create_app


def test_application_mounts_device_tool_endpoints() -> None:
    paths = create_app(Settings(app_env="test")).openapi()["paths"]
    assert "put" in paths["/v1/devices/{device_id}/tools"]
    assert "post" in paths["/v1/runs/{run_id}/device-tool-results"]


def test_error_code_is_registered_once() -> None:
    codes = [
        entry["code"]
        for entry in json.loads(Path("contracts/mvp/v1/error-codes.json").read_text("utf-8"))
    ]
    assert codes.count("device_call_not_pending") == 1
