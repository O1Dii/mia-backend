"""MW4 gate: the seams between M30, M40, M41, M42 and M50 in one application.

Each session proves its own DoD in its suite (``web/tests``, ``tests/plugins/linear``,
``tests/plugins/telegram``, ``tests/plugins/google_calendar``, ``tests/oauth``). This module
checks only what the wave integration joined:

- the production app mounts the M41 bot link and webhook next to the M50 authorization
  server, connected-apps API, OpenAI-compatible endpoints and ``/mcp``;
- app-facing endpoints answer an anonymous call with the protected-resource challenge
  (an OpenAI SDK or MCP client starts discovery from it);
- the Linear and Google Calendar packages pass the store's publication checks, and the
  builtin Telegram plugin is wired into the shared runtime.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.main import create_app
from app.modules.plugins.catalog.package import load_package
from app.modules.plugins.catalog.validation import package_problems
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]

MW4_PATHS = {
    # M41
    "/v1/telegram/link",
    "/webhooks/telegram",
    # M50
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/models",
    "/v1/apps",
    "/v1/oauth/requests/{handle}",
    "/v1/oauth/requests/{handle}/decision",
}


def test_production_app_mounts_every_mw4_surface() -> None:
    app = create_app(Settings(app_env="test"))
    with TestClient(app) as client:
        missing = MW4_PATHS - set(app.openapi()["paths"])
        assert not missing, missing
        assert app.state.telegram is not None and app.state.oauth is not None

        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        body = metadata.json()
        assert body["authorization_endpoint"].endswith("/oauth/authorize")
        assert body["code_challenge_methods_supported"] == ["S256"]

        for method, path in (("POST", "/v1/chat/completions"), ("POST", "/mcp")):
            response = client.request(method, path, json={})
            assert response.status_code == 401, (path, response.status_code)
            assert "resource_metadata" in response.headers["www-authenticate"]


def test_mw4_plugin_packages_pass_publication_checks() -> None:
    for directory in ("plugins/linear", "plugins/google-calendar"):
        assert package_problems(load_package(ROOT / directory)) == [], directory
