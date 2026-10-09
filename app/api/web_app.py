"""The built web app and the Android download, served by the API process (M62-T1).

A hosted Mia is one origin: ``/v1/...`` is the API, everything else under the public prefix is
the single-page app from ``WEB_DIST_DIR`` (``index.html``, ``oauth.html``, ``assets/``). The
proxy strips the prefix (``/mia/``), so routes here are prefix-free; the bundle itself is built
with the same base (``MIA_BASE_PATH``) and finds its assets and API under it.

``GET /downloads/mia.apk`` offers the Android build from a read-only volume
(``ANDROID_APK_PATH``); ``GET /downloads/mia.json`` describes it for the «Скачать» button.
Neither needs a session: the APK is public, like any app download page.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app.core.config import Settings

APK_MEDIA_TYPE = "application/vnd.android.package-archive"
APK_ROUTE = "/downloads/mia.apk"
APK_INFO_ROUTE = "/downloads/mia.json"
RESERVED_ROOTS = frozenset(
    {"v1", "health", "oauth", ".well-known", "mcp", "metrics", "downloads", "__e2e"}
)
"""First path segments that belong to the API: an unknown path there is a JSON 404, never HTML."""
HTML_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
MAX_INFO_BYTES = 4096


def _not_found(request: Request, message: str = "Not found.") -> JSONResponse:
    request_id = str(getattr(request.state, "request_id", "request"))
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "not_found",
                "message": message,
                "request_id": request_id,
                "retryable": False,
            }
        },
        headers={"Cache-Control": "no-cache"},
    )


def _apk_info(apk: Path) -> dict[str, Any] | None:
    """Size, time and the optional ``<apk>.json`` sidecar (``version_name``, ``version_code``)."""
    try:
        stat = apk.stat()
    except OSError:
        return None
    if not apk.is_file():
        return None
    info: dict[str, Any] = {
        "url": APK_ROUTE.lstrip("/"),
        "size_bytes": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat().replace("+00:00", "Z"),
        "version_name": None,
        "version_code": None,
    }
    sidecar = apk.with_name(apk.name + ".json")
    try:
        if sidecar.is_file() and sidecar.stat().st_size <= MAX_INFO_BYTES:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                name, code = data.get("version_name"), data.get("version_code")
                if isinstance(name, str) and 0 < len(name) <= 64:
                    info["version_name"] = name
                if isinstance(code, int) and not isinstance(code, bool) and code >= 0:
                    info["version_code"] = code
    except (OSError, ValueError):
        pass
    return info


def install_downloads(app: FastAPI, settings: Settings) -> None:
    apk = Path(settings.android_apk_path) if settings.android_apk_path else None

    @app.api_route(APK_ROUTE, methods=["GET", "HEAD"], include_in_schema=False)
    async def download_apk(request: Request) -> Response:
        if apk is None or not apk.is_file():
            return _not_found(request, "The Android app is not published here yet.")
        # FileResponse answers Range requests, so an interrupted download on mobile resumes.
        return FileResponse(
            apk,
            media_type=APK_MEDIA_TYPE,
            filename="mia.apk",
            content_disposition_type="attachment",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get(APK_INFO_ROUTE, include_in_schema=False)
    async def apk_info(request: Request) -> Response:
        info = _apk_info(apk) if apk is not None else None
        if info is None:
            return _not_found(request, "The Android app is not published here yet.")
        return JSONResponse(info, headers={"Cache-Control": "no-cache"})


class _ImmutableAssets(StaticFiles):
    """Vite file names carry a content hash, so assets are cacheable forever."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def install_web_app(app: FastAPI, settings: Settings) -> None:
    """Serve ``WEB_DIST_DIR``; must run after every API route (the SPA fallback is a catch-all)."""
    if settings.web_dist_dir is None:
        return
    root = Path(settings.web_dist_dir).resolve()
    index = root / "index.html"
    if not index.is_file():
        raise RuntimeError("WEB_DIST_DIR must contain the built index.html")
    # Only files present at start-up are served from the root; nothing is resolved per request.
    root_files = {entry.name: entry for entry in root.iterdir() if entry.is_file()}
    if (root / "assets").is_dir():
        app.mount("/assets", _ImmutableAssets(directory=root / "assets"), name="web-assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def web_page(path: str, request: Request) -> Response:
        first = path.split("/", 1)[0]
        if first in RESERVED_ROOTS:
            return _not_found(request)
        file = root_files.get(path)
        if file is not None and file.suffix != ".html":
            return FileResponse(
                file, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
            )
        # ``oauth.html`` is its own entry (M50 consent); every other page is the SPA shell.
        page = file if file is not None else index
        return FileResponse(page, media_type="text/html; charset=utf-8", headers=HTML_HEADERS)
