"""Provider adapters behind the Model Gateway."""

from __future__ import annotations

import httpx

from app.modules.models.adapters.base import (
    AdapterSettings,
    AdapterTarget,
    ModelAdapter,
    TokenSource,
)
from app.modules.models.adapters.chat_completions import ChatCompletionsAdapter
from app.modules.models.adapters.chat_only import ChatOnlyAdapter
from app.modules.models.adapters.responses import ResponsesAdapter


def build_adapter(
    client: httpx.AsyncClient, target: AdapterTarget, settings: AdapterSettings
) -> ModelAdapter:
    """Pick the adapter for a resolved connection by its provider spec."""
    kind = target.spec.adapter
    if kind == "responses_plan":
        return ResponsesAdapter(client, target, settings, plan_mode=True)
    if kind == "responses":
        return ResponsesAdapter(client, target, settings, plan_mode=False)
    if kind == "chat_completions":
        return ChatCompletionsAdapter(client, target, settings)
    return ChatOnlyAdapter(client, target, settings)


__all__ = [
    "AdapterSettings",
    "AdapterTarget",
    "ModelAdapter",
    "TokenSource",
    "build_adapter",
]
