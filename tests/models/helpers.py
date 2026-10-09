from __future__ import annotations

from collections.abc import Sequence

import httpx
from app.core.ports.model_gateway import (
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ToolSpec,
)
from app.modules.models.adapters import (
    AdapterSettings,
    AdapterTarget,
    ModelAdapter,
    build_adapter,
)
from app.modules.models.catalog import PROVIDERS
from pydantic import SecretStr

TOOLS = (
    ToolSpec(
        "get_time",
        "Return the current time in an IANA timezone.",
        {
            "type": "object",
            "properties": {"timezone": {"type": "string"}},
            "required": ["timezone"],
        },
    ),
    ToolSpec(
        "add_todo",
        "Add a todo item.",
        {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
    ),
)
USER_ID = "00000000-0000-0000-0000-000000000001"


class FakeTokens:
    """TokenSource that hands out numbered tokens and counts invalidations."""

    def __init__(self) -> None:
        self.issued = 0
        self.invalidated = 0

    async def access_token(self) -> str:
        self.issued += 1
        return f"plan-access-{self.issued}"

    async def invalidate(self) -> None:
        self.invalidated += 1


def make_request(
    *messages: ModelMessage, tools: Sequence[ToolSpec] = (), max_output_tokens: int | None = None
) -> ModelRequest:
    return ModelRequest(
        user_id=USER_ID,
        profile="chat",
        messages=list(messages) or [ModelMessage("user", "hi")],
        tools=tools,
        max_output_tokens=max_output_tokens,
    )


def make_adapter(
    provider: str,
    transport: httpx.AsyncBaseTransport,
    settings: AdapterSettings,
    *,
    model: str = "test-model",
    tokens: FakeTokens | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> tuple[ModelAdapter, httpx.AsyncClient]:
    spec = PROVIDERS[provider]
    target = AdapterTarget(
        spec=spec,
        model=model,
        base_url=base_url or spec.default_url,
        api_key=SecretStr(api_key) if api_key else None,
        tokens=tokens,
    )
    client = httpx.AsyncClient(transport=transport, trust_env=False)
    return build_adapter(client, target, settings), client


async def collect(adapter: ModelAdapter, request: ModelRequest) -> list[ModelEvent]:
    return [event async for event in adapter.stream(request)]


def assistant_with(call: ModelToolCall, content: str = "") -> ModelMessage:
    return ModelMessage("assistant", content, tool_calls=(call,))
