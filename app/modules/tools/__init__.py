"""Tools: the MVP tool router (M13) and the legacy audited executor (S17).

M13 public API: :class:`McpToolRouter` (``ToolRouter`` port), approvals
(``SqlApprovalService``/``InMemoryApprovalService``), ``InMemoryDeviceToolChannel``,
``CoreToolRegistry`` extension points, ``compute_tools_digest`` and
``install_tools`` (``app.modules.tools.composition``).
"""

from app.modules.tools.approvals import InMemoryApprovalService, SqlApprovalService
from app.modules.tools.core_tools import CoreTool, CoreToolRegistry, CoreToolResult
from app.modules.tools.device import InMemoryDeviceToolChannel
from app.modules.tools.digest import ServerTool, compute_tools_digest
from app.modules.tools.executor import ToolExecutor, ToolOutcome, ToolPrincipal
from app.modules.tools.registry import ToolRegistry, current_datetime_registry
from app.modules.tools.router import McpToolRouter, RouterConfig

__all__ = [
    "CoreTool",
    "CoreToolRegistry",
    "CoreToolResult",
    "InMemoryApprovalService",
    "InMemoryDeviceToolChannel",
    "McpToolRouter",
    "RouterConfig",
    "ServerTool",
    "SqlApprovalService",
    "ToolExecutor",
    "ToolOutcome",
    "ToolPrincipal",
    "ToolRegistry",
    "compute_tools_digest",
    "current_datetime_registry",
]
