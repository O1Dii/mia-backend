"""Shared tests fixtures."""

import os
from collections.abc import Generator

# LiteLLM (M50 test dependency) calls ``load_dotenv()`` on import unless it runs in
# production mode; that would pull a developer ``.env`` into the test process.
os.environ["LITELLM_MODE"] = "PRODUCTION"

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
