"""Health endpoint tests."""

from fastapi.testclient import TestClient


def test_live_health_returns_ok_and_request_id(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "test-request"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] == "test-request"


def test_ready_health_returns_service_unavailable_without_dependencies(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "service_unavailable"
    assert error["retryable"] is True
    assert response.headers["X-Request-ID"] == error["request_id"]
