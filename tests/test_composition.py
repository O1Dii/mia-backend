"""Wave W3 application composition checks."""

from app.core.config import Settings
from app.main import create_app


def test_create_app_mounts_wave_three_api_routes() -> None:
    application = create_app(Settings(app_env="test"))
    paths = set(application.openapi()["paths"])

    assert {
        "/v1/auth/google/nonce",
        "/v1/auth/google",
        "/v1/auth/refresh",
        "/v1/auth/logout",
        "/v1/me",
        "/v1/conversations",
        "/v1/conversations/{conversation_id}/messages:stream",
        "/v1/realtime/sessions",
    } <= paths
    assert str(application.url_path_for("realtime_websocket")) == "/v1/realtime"
