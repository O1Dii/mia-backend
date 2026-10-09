"""Public error codes emitted by the run engine and the runs API.

Registered in ``contracts/mvp/v1/error-codes.json`` (OI-M00-001); a test keeps
both in sync. Messages are fixed strings: provider and tool details never reach
clients.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class PublicCode:
    retryable: bool
    message: str
    http_status: int | None = None


RUN_CODES: Final[dict[str, PublicCode]] = {
    # Terminal run.failed errors.
    "run_internal_error": PublicCode(True, "The run failed unexpectedly."),
    "run_interrupted": PublicCode(True, "The run was interrupted before it finished."),
    "model_not_configured": PublicCode(False, "No model is connected for this account."),
    "invalid_request": PublicCode(False, "The model rejected the request."),
    "provider_unavailable": PublicCode(True, "The model provider is unavailable."),
    "provider_timeout": PublicCode(True, "The model provider did not answer in time."),
    "rate_limited": PublicCode(True, "The model provider limit was reached."),
    "context_too_long": PublicCode(False, "The conversation is too long for the model."),
    # tool.call.completed errors synthesized by the engine (router codes pass through).
    "tool_not_found": PublicCode(False, "The requested tool is not available."),
    "tool_budget_exceeded": PublicCode(False, "The tool call budget of this run is used up."),
    "approval_unavailable": PublicCode(False, "Approvals are not available for this run."),
    "tool_error": PublicCode(False, "The tool call did not succeed."),
    "approval_denied": PublicCode(False, "The action was not approved."),
    "approval_expired": PublicCode(False, "The approval expired."),
    "approval_cancelled": PublicCode(False, "The approval was cancelled."),
    # HTTP errors of /v1/runs*.
    "run_not_found": PublicCode(False, "The run was not found.", 404),
    "invalid_cursor": PublicCode(False, "The event cursor is invalid.", 400),
    "voice_feed_only": PublicCode(False, "Voice runs always use the main feed.", 422),
}

TOOL_STATUS_MESSAGES: Final[dict[str, str]] = {
    "failed": "The tool call failed.",
    "denied": "The action was not approved.",
    "expired": "The approval expired.",
    "uncertain": "The action may or may not have happened.",
    "cancelled": "The tool call was cancelled.",
}

_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def public_error(code: str) -> dict[str, Any]:
    """``common.schema.json#/$defs/publicError`` for a run-engine code."""
    known = RUN_CODES.get(code) or RUN_CODES["run_internal_error"]
    if code not in RUN_CODES:
        code = "run_internal_error"
    return {"code": code, "message": known.message, "retryable": known.retryable}


def tool_error(status: str, code: str | None) -> dict[str, Any]:
    """Error of ``tool.call.completed``: router codes pass through when well-formed."""
    safe = code if code is not None and _CODE.match(code) else "tool_error"
    known = RUN_CODES.get(safe)
    message = known.message if known else TOOL_STATUS_MESSAGES.get(status, "Tool call failed.")
    return {"code": safe, "message": message, "retryable": False}
