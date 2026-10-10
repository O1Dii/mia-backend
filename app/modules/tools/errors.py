"""Public error codes of the tool router and the approvals API (owner M13).

Registered in ``contracts/mvp/v1/error-codes.json``; ``tests/tools`` keeps both
in sync. Codes travel as ``ToolOutcome.error_code`` into
``tool.call.completed.error`` (run engine) or as the HTTP error envelope of
``/v1/approvals*``. Messages are fixed strings and never carry tool output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class RouterCode:
    retryable: bool
    message: str
    http_status: int | None = None


ROUTER_CODES: Final[dict[str, RouterCode]] = {
    # Policy (nothing executed).
    "tool_invalid_arguments": RouterCode(False, "The tool arguments are invalid."),
    "tool_permission_denied": RouterCode(False, "The tool call is not permitted."),
    "tool_consent_required": RouterCode(
        False, "The plugin changed its tools; review and consent again."
    ),
    "tool_not_active": RouterCode(False, "The plugin is not active."),
    "connection_required": RouterCode(False, "The plugin needs a connected account."),
    "tool_host_blocked": RouterCode(False, "The plugin server address is not allowed."),
    # Execution.
    "tool_unavailable": RouterCode(True, "The plugin server is unavailable."),
    "tool_timeout": RouterCode(False, "The tool did not answer in time."),
    "tool_execution_failed": RouterCode(True, "The tool could not complete the request."),
    # Approvals bound to exact arguments.
    "approval_not_found": RouterCode(False, "The approval was not found.", 404),
    "approval_args_mismatch": RouterCode(
        False, "The arguments differ from the approved ones.", 409
    ),
    "approval_already_decided": RouterCode(False, "The approval was already decided.", 409),
    "approval_mismatch": RouterCode(False, "The approval belongs to another action."),
    "approval_pending": RouterCode(False, "The approval is still pending."),
    "approval_already_used": RouterCode(False, "The approval was already used."),
    # Device tools (DeviceToolChannel).
    "device_unavailable": RouterCode(False, "No connected device can run this tool."),
    "device_unsupported_tool": RouterCode(False, "The device does not support this tool."),
    "device_invalid_arguments": RouterCode(False, "The device rejected the arguments."),
    "device_os_permission_denied": RouterCode(False, "The device permission is missing."),
    "device_locked": RouterCode(False, "Unlock the device to continue."),
    "device_user_cancelled": RouterCode(False, "The action was cancelled on the device."),
    "device_timeout": RouterCode(False, "The device did not answer in time."),
    "device_execution_failed": RouterCode(False, "The device could not complete the action."),
}
"""Codes owned by M13. ``approval_expired``/``approval_denied``/``approval_cancelled``/
``approval_unavailable``/``tool_not_found`` are M12 codes reused by the router."""


DEVICE_ACCESS_CODES: Final[dict[str, RouterCode]] = {
    # Router outcomes of device calls from other clients (ADR-0016).
    "device_offline": RouterCode(False, "The phone is not reachable right now."),
    "device_capability_disabled": RouterCode(
        False, "This action is turned off for other clients on the phone."
    ),
    # Approvals and device access API.
    "approval_device_only": RouterCode(False, "Confirm this request on the phone.", 403),
    "approval_scope_unavailable": RouterCode(
        False, "This consent cannot be given permanently.", 409
    ),
    "device_access_forbidden": RouterCode(
        False, "Only the app on this phone can manage its access.", 403
    ),
    "device_grant_not_found": RouterCode(False, "The permission was not found.", 404),
    "device_capability_unknown": RouterCode(False, "The capability is unknown.", 404),
}
"""Codes owned by M70 (device access, ADR-0016)."""


def router_message(code: str) -> str:
    known = ROUTER_CODES.get(code) or DEVICE_ACCESS_CODES.get(code)
    return known.message if known else "The tool call did not succeed."
