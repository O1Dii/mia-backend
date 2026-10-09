"""The API process serves the built web app and the Android download (M62-T1)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app.core.config import Settings
from app.main import create_app

INDEX = '<!doctype html><div id="root"></div>'


def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "oauth.html").write_text("<!doctype html>consent")
    (root / "favicon.svg").write_text("<svg/>")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside the web root")
    return root


def client_for(settings: Settings) -> httpx.AsyncClient:
    app = create_app(settings, install_modules=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mia.test")


async def test_spa_pages_assets_and_api_paths(tmp_path: Path) -> None:
    settings = Settings(app_env="test", web_dist_dir=str(dist(tmp_path)))
    async with client_for(settings) as client:
        for path in ("/", "/chat", "/store/connected?connection=ok", "/models"):
            page = await client.get(path)
            assert page.status_code == 200 and page.text == INDEX, path
            assert page.headers["content-type"].startswith("text/html")
            assert page.headers["cache-control"] == "no-cache"
            assert page.headers["x-frame-options"] == "DENY"
        assert (await client.get("/oauth.html")).text.endswith("consent")
        asset = await client.get("/assets/index-abc123.js")
        assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
        assert (await client.get("/favicon.svg")).text == "<svg/>"
        assert (await client.head("/chat")).status_code == 200

        # API space never falls back to HTML; health still answers.
        for path in ("/v1/unknown", "/oauth/unknown", "/.well-known/nothing", "/downloads/x"):
            missing = await client.get(path)
            assert missing.status_code == 404, path
            assert missing.json()["error"]["code"] == "not_found"
        assert (await client.get("/health/live")).json() == {"status": "ok"}

        # Nothing outside the web root is reachable.
        for path in ("/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt"):
            response = await client.get(path)
            assert "outside the web root" not in response.text, path


async def test_without_a_web_dist_the_api_has_no_catch_all() -> None:
    async with client_for(Settings(app_env="test")) as client:
        assert (await client.get("/chat")).status_code == 404


def test_a_dist_without_index_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        create_app(Settings(app_env="test", web_dist_dir=str(tmp_path)), install_modules=False)


async def test_apk_download_headers_range_and_info(tmp_path: Path) -> None:
    apk = tmp_path / "releases" / "mia.apk"
    apk.parent.mkdir()
    apk.write_bytes(b"PK\x03\x04" + b"x" * 1020)
    settings = Settings(app_env="test", android_apk_path=str(apk))
    async with client_for(settings) as client:
        download = await client.get("/downloads/mia.apk")
        assert download.status_code == 200 and len(download.content) == 1024
        assert download.headers["content-type"] == "application/vnd.android.package-archive"
        assert download.headers["content-disposition"] == 'attachment; filename="mia.apk"'
        assert download.headers["cache-control"] == "no-cache"
        part = await client.get("/downloads/mia.apk", headers={"Range": "bytes=0-3"})
        assert part.status_code == 206 and part.content == b"PK\x03\x04"
        assert (await client.head("/downloads/mia.apk")).status_code == 200

        info = (await client.get("/downloads/mia.json")).json()
        assert info["size_bytes"] == 1024 and info["url"] == "downloads/mia.apk"
        assert info["version_name"] is None
        apk.with_name("mia.apk.json").write_text(
            json.dumps({"version_name": "0.1.0 (c23e2e5)", "version_code": 1})
        )
        info = (await client.get("/downloads/mia.json")).json()
        assert (info["version_name"], info["version_code"]) == ("0.1.0 (c23e2e5)", 1)

        apk.unlink()
        gone = await client.get("/downloads/mia.apk")
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "not_found"
        assert (await client.get("/downloads/mia.json")).status_code == 404


async def test_apk_routes_answer_404_when_not_configured() -> None:
    async with client_for(Settings(app_env="test")) as client:
        assert (await client.get("/downloads/mia.apk")).status_code == 404
        assert (await client.get("/downloads/mia.json")).status_code == 404


def test_public_path_prefix_follows_the_public_base_url() -> None:
    assert Settings(app_env="test").public_path_prefix == ""
    hosted = Settings(app_env="test", public_base_url="https://mindmapgen.xyz/mia/")
    assert hosted.public_path_prefix == "/mia"
