"""Validate documents against ``contracts/mvp/v1`` from tests.

Later sessions use ``assert_mvp_valid`` to check what they emit, e.g.
``assert_mvp_valid(event, "run-events")`` or
``assert_mvp_valid(body, "approval", "ApprovalDecisionRequest")``.
The logic lives in ``contracts/scripts/validate_contracts.py``; this module
only loads it.
"""

from __future__ import annotations

import importlib.util
import json
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MVP = ROOT / "contracts/mvp/v1"


@cache
def validator_module() -> ModuleType:
    """The repository contract validator, imported from its file path."""
    path = ROOT / "contracts/scripts/validate_contracts.py"
    spec = importlib.util.spec_from_file_location("mia_validate_contracts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@cache
def _registry() -> object:
    module = validator_module()
    registry: object = module.mvp_registry(module.mvp_schemas())
    return registry


def mvp_errors(document: object, stem: str, definition: str | None = None) -> list[str]:
    """Error messages of ``document`` against ``<stem>.schema.json[#/$defs/<definition>]``."""
    validator = validator_module().mvp_validator(_registry(), stem, definition)
    return [str(error.message) for error in validator.iter_errors(document)]


def assert_mvp_valid(document: object, stem: str, definition: str | None = None) -> None:
    errors = mvp_errors(document, stem, definition)
    target = stem if definition is None else f"{stem}#/$defs/{definition}"
    assert not errors, f"not a valid {target}: {errors}"


def load_mvp_example(relative: str) -> Any:
    """Load ``contracts/mvp/v1/examples/<relative>``."""
    return json.loads((MVP / "examples" / relative).read_text(encoding="utf-8"))
