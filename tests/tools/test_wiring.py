"""Composition and the public error-code registry of the tool router."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.main import create_app
from app.modules.runs.errors import RUN_CODES
from app.modules.tools.composition import ToolStack, host_allowlist
from app.modules.tools.core_tools import RESERVED_NAMESPACES
from app.modules.tools.errors import ROUTER_CODES
from app.modules.tools.router import McpToolRouter

REGISTRY = Path("contracts/mvp/v1/error-codes.json")


def test_router_codes_are_registered_and_disjoint_from_run_codes() -> None:
    registry = {entry["code"]: entry for entry in json.loads(REGISTRY.read_text("utf-8"))}
    for code, known in ROUTER_CODES.items():
        entry = registry[code]
        assert entry["owner"] == "M13"
        assert (entry["retryable"], entry["public_message"], entry["http_status"]) == (
            known.retryable,
            known.message,
            known.http_status,
        )
    assert not set(ROUTER_CODES) & set(RUN_CODES)


def test_application_wires_router_approvals_and_device_channel() -> None:
    app = create_app(Settings(app_env="test"))
    stack = app.state.tools
    assert isinstance(stack, ToolStack) and isinstance(stack.router, McpToolRouter)
    assert app.state.runs.engine.router is stack.router
    assert app.state.runs.engine.approvals is stack.approvals
    paths = set(app.openapi()["paths"])
    assert {
        "/v1/approvals",
        "/v1/approvals/{approval_id}",
        "/v1/approvals/{approval_id}/decision",
    } <= paths
    # Core tools are registered by M16/M17 only inside the reserved namespaces.
    assert all(t.name.split(".")[0] in RESERVED_NAMESPACES for t in stack.core_tools.tools())


def test_host_allowlist_setting() -> None:
    assert host_allowlist(Settings(app_env="test")) is None
    parsed = host_allowlist(
        Settings(app_env="test", tools_mcp_host_allowlist=" MCP.Linear.app , ,x.example")
    )
    assert parsed == frozenset({"mcp.linear.app", "x.example"})
