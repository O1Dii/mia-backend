"""Fixtures for the composed-application tool journeys."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.e2e_tools import harness


@pytest.fixture
def backend() -> Iterator[None]:
    """Migrate to head and seed exactly one authenticated user and device."""
    harness.reset_backend()
    yield
