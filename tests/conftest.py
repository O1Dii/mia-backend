"""Shared tests fixtures."""

from collections.abc import Generator

import pytest
from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Serve the app with an explicit test configuration."""
    application = create_app(Settings(app_env="test"))
    with TestClient(application) as test_client:
        yield test_client
