"""MW5 gate: the seams between M24, M31 and M43 in one application.

Each session proves its own DoD in its suite (``web/tests``, ``tests/tiles``,
``tests/web_handoff``, ``tests/plugin_template``; Android ``:core:push``/``:feature:web``;
MindWeaver ``backend/e2e_mia``). This module checks only what the wave integration joined:

- the production app mounts the M31 tile host next to the M24 WebView hand-off;
- every page the hand-off may land on is a route of the M31 web app, and every web surface
  of the Android app (M24) is such a page — the WebView never lands on "page not found";
- the web POC is gone from the application package (M31);
- the M43 plugin template passes the store's publication checks.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from app.core.config import Settings
from app.main import create_app
from app.modules.auth.web_handoff import ALLOWED_LANDING_ROOTS
from app.modules.plugins.catalog.package import load_package
from app.modules.plugins.catalog.validation import package_problems

ROOT = Path(__file__).resolve().parents[2]

MW5_PATHS = {
    # M24
    "/v1/auth/web/handoff",
    "/v1/auth/web/handoff/redeem",
    # M31
    "/v1/installations/{installation_id}/tiles/{tile_id}",
    "/v1/installations/{installation_id}/tiles/{tile_id}/tool-calls",
}


def _web_app_roots() -> set[str]:
    source = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    return set(re.findall(r"path(?:\.startsWith\(| === )'/([a-z-]+)", source))


def test_production_app_mounts_every_mw5_surface() -> None:
    app = create_app(Settings(app_env="test"))
    missing = MW5_PATHS - set(app.openapi()["paths"])
    assert not missing, missing


def test_handoff_lands_only_on_pages_of_the_web_app() -> None:
    roots = _web_app_roots()
    assert {"dashboard", "memory", "inbox", "data"} <= roots, roots
    assert ALLOWED_LANDING_ROOTS <= roots, ALLOWED_LANDING_ROOTS - roots


def test_android_web_surfaces_are_allowed_landing_pages() -> None:
    android = os.environ.get("MIA_ANDROID_DIR")
    if not android:
        pytest.skip("set MIA_ANDROID_DIR to the Android checkout to check its web surfaces")
    surface = next(Path(android, "feature/web/src/main").rglob("WebSurface.kt"))
    paths = re.findall(r'"(/[a-z-]+)"', surface.read_text(encoding="utf-8"))
    assert paths, surface
    assert {path.lstrip("/") for path in paths} <= ALLOWED_LANDING_ROOTS, paths


def test_web_poc_is_removed() -> None:
    # Stale ``__pycache__`` in a developer checkout is not source; only ``.py`` files count.
    for directory in ("app/web_poc", "tests/web_poc"):
        assert not list((ROOT / directory).glob("**/*.py")), directory


def test_plugin_template_passes_publication_checks() -> None:
    assert package_problems(load_package(ROOT / "plugins/_template")) == []
