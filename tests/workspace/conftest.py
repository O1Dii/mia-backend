from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.workspace.harness import Harness, build_harness


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async with build_harness() as value:
        yield value
