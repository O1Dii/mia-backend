"""In-memory fakes of the MVP core ports (``app.core.ports``).

They follow the documented port semantics closely enough for run-engine and
API tests (M12+) to rely on them. They are not reference implementations of
persistence, security or concurrency.
"""

from tests.fakes.approvals import FakeApprovalService
from tests.fakes.contracts import assert_mvp_valid, load_mvp_example, mvp_errors
from tests.fakes.device_tools import FakeDeviceToolChannel
from tests.fakes.memory import FakeMemoryProvider, FakePerson
from tests.fakes.model_gateway import FakeModelGateway, text_step, tool_step
from tests.fakes.tool_router import FakeToolRouter, ToolHandler

__all__ = [
    "FakeApprovalService",
    "FakeDeviceToolChannel",
    "FakeMemoryProvider",
    "FakeModelGateway",
    "FakePerson",
    "FakeToolRouter",
    "ToolHandler",
    "assert_mvp_valid",
    "load_mvp_example",
    "mvp_errors",
    "text_step",
    "tool_step",
]
