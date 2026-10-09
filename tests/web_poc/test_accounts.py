"""Hosted access, Google identity, persistence, review ownership and deployment boundaries."""

import secrets
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from app.modules.auth.google import GoogleIdentity, InvalidGoogleTokenError
from app.web_poc.accounts import WebSettings
from app.web_poc.main import AUTH_COOKIE, create_web_app
from fastapi.testclient import TestClient

ORIGIN = "https://mia.example"
HEADERS = {"X-Mia-Web": "1", "Origin": ORIGIN}


class Google:
    subject = "google-person-1"
    reject = False

    async def verify(self, id_token: str, *, audience: str, nonce: str) -> GoogleIdentity:
        assert audience == "public-client-id" and len(nonce) > 20 and len(id_token) > 20
        if self.reject:
            raise InvalidGoogleTokenError("redacted")
        return GoogleIdentity(
            self.subject, "person@example.org", True, "Тестовый пользователь", None, "ru"
        )


def settings(path: Path, **kw: Any) -> WebSettings:
    return WebSettings(
        public_origin=ORIGIN, google_client_id="public-client-id", database_path=str(path), **kw
    )


def sign_in(client: TestClient) -> dict[str, Any]:
    nonce = client.post("/api/web/auth/nonce", headers=HEADERS, json={})
    assert nonce.status_code == 200
    result = client.post(
        "/api/web/auth/google", headers=HEADERS, json={"credential": secrets.token_urlsafe(40)}
    )
    assert result.status_code == 200, result.text
    return dict(result.json()["user"])


def test_hosted_requires_sign_in_and_only_exposes_safe_auth_configuration(tmp_path: Path) -> None:
    with TestClient(
        create_web_app(settings=settings(tmp_path / "db.sqlite")),
        base_url=ORIGIN,
        client=("203.0.113.10", 1234),
    ) as client:
        assert client.get("/api/web/health").status_code == 200
        session = client.get("/api/web/auth/session").json()
        assert session["required"] and session["user"] is None
        assert client.get("/api/web/workspace").status_code == 401
        assert client.get("/api/web/connections").status_code == 401
        assert (
            client.post(
                "/api/web/actions", headers=HEADERS, json={"name": "mia_timer__get"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/web/auth/nonce",
                headers={"X-Mia-Web": "1", "Origin": "https://evil.example"},
                json={},
            ).status_code
            == 403
        )
        assert client.post("/api/web/auth/nonce", json={}).status_code == 403
        assert client.get("/api/web/workspace", headers={"Host": "evil.example"}).status_code == 400


def test_google_redirect_can_open_the_page_but_not_cross_site_api_requests(
    tmp_path: Path,
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>MIA return</title>")
    (static / "asset.js").write_text("// same-origin asset")
    navigation = {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    with TestClient(
        create_web_app(
            settings=settings(tmp_path / "db.sqlite", static_dir=str(static), google_hub=True)
        ),
        base_url=ORIGIN,
    ) as client:
        landing = client.get("/?signin=google", headers=navigation)
        assert landing.status_code == 200
        assert "MIA return" in landing.text
        assert client.get("/api/web/auth/session").json()["user"] is None
        assert (
            client.head(
                "/", headers={**navigation, "Origin": "https://accounts.google.com"}
            ).status_code
            == 200
        )
        for path in ("/api/web/auth/session", "/api/web/workspace", "/asset.js"):
            assert client.get(path, headers=navigation).status_code == 403
        for overrides in (
            {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
            {"Sec-Fetch-Dest": "iframe"},
        ):
            assert client.get("/", headers={**navigation, **overrides}).status_code == 403
        for path in ("/", "/api/web/auth/hub", "/api/web/auth/logout"):
            assert client.post(path, headers={**HEADERS, **navigation}, json={}).status_code == 403
        assert client.post("/api/web/auth/hub", headers=HEADERS, json={}).status_code == 401


def test_google_nonce_is_cookie_bound_single_use_and_session_revocable(tmp_path: Path) -> None:
    google = Google()
    with TestClient(
        create_web_app(settings=settings(tmp_path / "db.sqlite"), google_verifier=google),
        base_url=ORIGIN,
    ) as client:
        assert (
            client.post(
                "/api/web/auth/google", headers=HEADERS, json={"credential": "x" * 40}
            ).status_code
            == 401
        )
        user = sign_in(client)
        assert client.get("/api/web/workspace").json()["subject_id"] == user["id"]
        assert (
            client.post(
                "/api/web/auth/google", headers=HEADERS, json={"credential": "x" * 40}
            ).status_code
            == 401
        )
        cookie = client.cookies.get(AUTH_COOKIE)
        assert cookie is not None
        result = client.post("/api/web/auth/logout", headers=HEADERS, json={})
        assert result.status_code == 200
        assert client.get("/api/web/workspace").status_code == 401
        assert cookie is not None
        client.cookies.set(AUTH_COOKIE, cookie)
        assert client.get("/api/web/workspace").status_code == 401
        google.reject = True
        client.post("/api/web/auth/nonce", headers=HEADERS, json={})
        assert (
            client.post(
                "/api/web/auth/google", headers=HEADERS, json={"credential": "x" * 40}
            ).status_code
            == 401
        )


def test_account_state_and_reviews_survive_restart_without_provider_credentials(
    tmp_path: Path,
) -> None:
    db = tmp_path / "db.sqlite"
    google = Google()
    cfg = settings(db)
    with TestClient(
        create_web_app(settings=cfg, google_verifier=google), base_url=ORIGIN
    ) as client:
        user = sign_in(client)
        client.post("/api/web/plugins/install", headers=HEADERS, json={"plugin_id": "mia.notes"})
        client.post(
            "/api/web/actions",
            headers=HEADERS,
            json={"name": "mia_notes__add", "arguments": {"text": "Сохранённая задача"}},
        )
        synthetic = secrets.token_urlsafe(40)
        assert (
            client.post(
                "/api/web/connections",
                headers=HEADERS,
                json={
                    "provider": "openai",
                    "model": "test",
                    "label": "Account model",
                    "api_key": synthetic,
                },
            ).status_code
            == 201
        )
        review = client.post(
            "/api/web/plugins/mia.notes/reviews",
            headers=HEADERS,
            json={"rating": 4, "text": "Удобно сохранять задачи."},
        )
        assert review.status_code == 200
        assert "person@example.org" not in review.text
        cookie = client.cookies.get(AUTH_COOKIE)
        assert cookie is not None
    # Read only the synthetic test DB via a new running application, never deployed data.
    with TestClient(
        create_web_app(settings=cfg, google_verifier=google), base_url=ORIGIN
    ) as client:
        assert cookie is not None
        client.cookies.set(AUTH_COOKIE, cookie)
        state = client.get("/api/web/workspace").json()
        assert state["subject_id"] == user["id"]
        assert state["states"]["mia.notes"]["notes"][0]["text"] == "Сохранённая задача"
        assert state["layout"][0]["plugin_id"] == "mia.notes"
        assert client.get("/api/web/connections").json() == []
        assert client.get("/api/web/plugins/mia.notes/reviews").json()["count"] == 1
        client.post(
            "/api/web/plugins/mia.notes/reviews",
            headers=HEADERS,
            json={"rating": 5, "text": "Исправленный отзыв."},
        )
        reviews = client.get("/api/web/plugins/mia.notes/reviews").json()
        assert reviews["count"] == 1 and reviews["average"] == 5
        client.delete("/api/web/plugins/mia.notes/reviews", headers=HEADERS)
        assert client.get("/api/web/plugins/mia.notes/reviews").json()["count"] == 0


def test_reviews_require_installation_and_cannot_be_deleted_by_another_user(tmp_path: Path) -> None:
    google = Google()
    app = create_web_app(settings=settings(tmp_path / "db.sqlite"), google_verifier=google)
    with TestClient(app, base_url=ORIGIN) as owner:
        sign_in(owner)
        review = {"rating": 5, "text": "Полезный плагин."}
        assert (
            owner.post(
                "/api/web/plugins/mia.timer/reviews", headers=HEADERS, json=review
            ).status_code
            == 403
        )
        owner.post("/api/web/plugins/install", headers=HEADERS, json={"plugin_id": "mia.timer"})
        owner.post("/api/web/plugins/mia.timer/reviews", headers=HEADERS, json=review)
        assert (
            owner.post(
                "/api/web/plugins/mia.timer/reviews", headers=HEADERS, json={**review, "rating": 6}
            ).status_code
            == 422
        )
        # A separate cookie jar without nesting the same app lifespan.
        with httpx.Client():
            saved = owner.cookies.get(AUTH_COOKIE)
            assert saved is not None
            owner.cookies.clear()
            google.subject = "another-google-person"
            other = sign_in(owner)
            assert owner.get("/api/web/workspace").json()["installed"] == []
            owner.delete("/api/web/plugins/mia.timer/reviews", headers=HEADERS)
            reviews = owner.get("/api/web/plugins/mia.timer/reviews").json()
            assert reviews["count"] == 1 and not reviews["items"][0]["mine"]
            owner.cookies.clear()
            assert saved is not None
            owner.cookies.set(AUTH_COOKIE, saved)
            assert owner.get("/api/web/auth/session").json()["user"]["id"] != other["id"]


def test_shared_google_hub_is_verified_server_side_and_never_accepts_local_accounts(
    tmp_path: Path,
) -> None:
    valid = True

    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ORIGIN + "/api/auth/me"
        assert request.headers["cookie"] == "mw_session=synthetic-cookie"
        assert "mia_account" not in request.headers["cookie"]
        return httpx.Response(
            200,
            json={
                "authenticated": True,
                "google_enabled": True,
                "user": {
                    "id": str(uuid4()),
                    "name": "Google user",
                    "email": "user@example.org",
                    "is_local": not valid,
                },
            },
        )

    cfg = settings(tmp_path / "db.sqlite", google_hub=True)
    with TestClient(
        create_web_app(settings=cfg, transport=httpx.MockTransport(upstream)), base_url=ORIGIN
    ) as client:
        assert client.post("/api/web/auth/hub", headers=HEADERS, json={}).status_code == 401
        client.cookies.set("mw_session", "synthetic-cookie")
        assert client.post("/api/web/auth/hub", headers=HEADERS, json={}).status_code == 200
        assert client.get("/api/web/auth/session").json()["user"]["name"] == "Google user"
        client.post("/api/web/auth/logout", headers=HEADERS, json={})
        valid = False
        assert client.post("/api/web/auth/hub", headers=HEADERS, json={}).status_code == 401
        assert client.get("/api/web/workspace").status_code == 401


def test_public_deployment_never_exposes_server_loopback_as_user_model(tmp_path: Path) -> None:
    with TestClient(
        create_web_app(settings=settings(tmp_path / "db.sqlite"), google_verifier=Google()),
        base_url=ORIGIN,
    ) as client:
        sign_in(client)
        result = client.post(
            "/api/web/connections",
            headers=HEADERS,
            json={
                "provider": "local",
                "model": "anything",
                "label": "loopback",
                "base_url": "http://127.0.0.1:8000/v1",
            },
        )
        assert result.status_code == 422


def test_public_config_fails_closed_and_subpath_cookies_are_secure(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_web_app(settings=WebSettings(public_origin=ORIGIN))
    with pytest.raises(ValueError):
        create_web_app(
            settings=WebSettings(
                public_origin="http://example.org",
                google_client_id="test",
                database_path=str(tmp_path / "db"),
            )
        )
    cfg = settings(tmp_path / "cookie.db", base_path="/mia")
    with TestClient(
        create_web_app(settings=cfg, google_verifier=Google()), base_url=ORIGIN
    ) as client:
        nonce = client.post("/api/web/auth/nonce", headers=HEADERS, json={})
        cookie = nonce.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/mia/" in cookie
