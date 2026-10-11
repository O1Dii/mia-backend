"""Fixtures of the account tests (M17-R1): the production composition of the MVP acceptance."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.e2e_mvp.harness import Mvp, mvp_world


@pytest.fixture
async def mvp(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Mvp]:
    async with mvp_world(monkeypatch) as world:
        yield world
