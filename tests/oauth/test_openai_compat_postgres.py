"""OpenAI-compatible API through Mia (M50 DoD): the OpenAI Python SDK and LiteLLM with
``base_url`` = Mia, memory context without raw facts, spending limits, Mia tools.

The SDK clients talk to the app in process (``httpx.ASGITransport``); the model is scripted.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelGatewayError,
    ModelToolCall,
    ModelUsage,
    TextDelta,
    ToolCallRequested,
)
from app.modules.memory.models import MemoryAccessLog
from sqlalchemy import select

from tests.oauth.harness import ISSUER, OAuthWorld, oauth_world
from tests.runs.conftest import requires_postgres
from tests.tools.helpers import build_world

pytestmark = requires_postgres

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ["LITELLM_MODE"] = "PRODUCTION"  # never load_dotenv() on import


def answer(*chunks: str, cost: float | None = None, tokens: tuple[int, int] = (12, 4)) -> list[Any]:
    return [
        *(TextDelta(c) for c in chunks),
        ModelUsage(input_tokens=tokens[0], output_tokens=tokens[1], cost_usd=cost),
        ModelDone("stop"),
    ]


def calls(name: str, arguments: dict[str, Any], *, cost: float | None = None) -> list[ModelEvent]:
    return [
        ToolCallRequested(ModelToolCall(call_id="call_1", name=name, arguments=arguments)),
        ModelUsage(input_tokens=20, output_tokens=5, cost_usd=cost),
        ModelDone("tool_calls"),
    ]


@pytest.fixture
async def world() -> AsyncIterator[OAuthWorld]:
    async with oauth_world() as built:
        yield built


def sdk(world: OAuthWorld, token: str) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        base_url=f"{ISSUER}/v1",
        api_key=token,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app)),
    )


async def access(world: OAuthWorld, scope: str = "llm.generate", **kwargs: Any) -> str:
    async with world.http() as http:
        tokens = await world.login(http, scope=scope, **kwargs)
    return str(tokens["access_token"])


async def grant_id(world: OAuthWorld) -> str:
    async with world.http() as http:
        apps = await http.get("/v1/apps", headers={"x-test-user": str(world.user)})
    return str(apps.json()["items"][0]["id"])


# -- the OpenAI Python SDK ---------------------------------------------------------------------


async def test_openai_sdk_chat_completion(world: OAuthWorld) -> None:
    client = sdk(world, await access(world))
    world.script(answer("Привет", ", мир!"))
    response = await client.chat.completions.create(
        model="mia:default",
        messages=[
            {"role": "system", "content": "You are a mind-map generator."},
            {"role": "user", "content": "Карта про кофе"},
        ],
    )
    assert response.choices[0].message.content == "Привет, мир!"
    assert response.choices[0].finish_reason == "stop"
    assert response.usage is not None and response.usage.prompt_tokens == 12
    request = world.gateway.requests[0]
    assert request.profile == "app" and request.model == "mia:default"
    assert [(m.role, m.content) for m in request.messages] == [
        ("system", "You are a mind-map generator."),
        ("user", "Карта про кофе"),
    ]
    assert request.tools == () and request.structured_response is False


async def test_openai_sdk_streaming_chat_completion(world: OAuthWorld) -> None:
    client = sdk(world, await access(world))
    world.script(answer("Раз", " два", " три"))
    stream = await client.chat.completions.create(
        model="mia:default",
        messages=[{"role": "user", "content": "Считай"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    text, usage, finish = "", None, None
    async for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
            finish = chunk.choices[0].finish_reason or finish
        if chunk.usage:
            usage = chunk.usage
    assert text == "Раз два три" and finish == "stop"
    assert usage is not None and usage.completion_tokens == 4


async def test_openai_sdk_responses_api(world: OAuthWorld) -> None:
    client = sdk(world, await access(world))
    world.script(answer("Готово."), answer("По", "токам"))
    response = await client.responses.create(
        model="mia:default", instructions="Be brief.", input="Сделай"
    )
    assert response.output_text == "Готово."
    assert [(m.role, m.content) for m in world.gateway.requests[0].messages] == [
        ("system", "Be brief."),
        ("user", "Сделай"),
    ]
    stream = await client.responses.create(model="mia:default", input="Ещё", stream=True)
    kinds, deltas, final = [], [], None
    async for event in stream:
        kinds.append(event.type)
        if event.type == "response.output_text.delta":
            deltas.append(event.delta)
        if event.type == "response.completed":
            final = event.response
    assert "".join(deltas) == "Потокам"
    assert kinds[0] == "response.created" and kinds[-1] == "response.completed"
    assert final is not None and final.output_text == "Потокам"


async def test_openai_sdk_models_and_client_tools(world: OAuthWorld) -> None:
    client = sdk(world, await access(world))
    models = await client.models.list()
    assert [m.id for m in models.data] == ["mia:default"]
    world.script(calls("get_weather", {"city": "Minsk"}), answer("В Минске +5."))
    tools: Any = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Weather by city",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    first = await client.chat.completions.create(
        model="mia:default", messages=[{"role": "user", "content": "Погода?"}], tools=tools
    )
    call = first.choices[0].message.tool_calls
    assert first.choices[0].finish_reason == "tool_calls"
    assert call is not None and call[0].function.name == "get_weather"  # type: ignore[union-attr]
    assert call[0].function.arguments == '{"city": "Minsk"}'  # type: ignore[union-attr]
    second = await client.chat.completions.create(
        model="mia:default",
        messages=[
            {"role": "user", "content": "Погода?"},
            first.choices[0].message.model_dump(exclude_none=True),  # type: ignore[list-item]
            {"role": "tool", "tool_call_id": call[0].id, "content": "+5"},
        ],
        tools=tools,
    )
    assert second.choices[0].message.content == "В Минске +5."
    replayed = world.gateway.requests[1].messages
    assert replayed[1].tool_calls[0].name == "get_weather"
    assert replayed[2].role == "tool" and replayed[2].tool_call_id == call[0].id


async def test_errors_use_the_v1_envelope(world: OAuthWorld) -> None:
    client = sdk(world, await access(world))
    with pytest.raises(openai.BadRequestError) as unsupported:
        await client.chat.completions.create(
            model="gpt-5", messages=[{"role": "user", "content": "x"}]
        )
    assert unsupported.value.code == "unsupported_model"
    world.script(ModelGatewayError("model_not_configured", "no model"))
    with pytest.raises(openai.ConflictError) as missing:
        await client.chat.completions.create(
            model="mia:default", messages=[{"role": "user", "content": "x"}]
        )
    assert missing.value.code == "model_not_configured"
    bad = openai.AsyncOpenAI(
        base_url=f"{ISSUER}/v1",
        api_key="mia_at_" + "x" * 43,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app)),
    )
    with pytest.raises(openai.AuthenticationError):
        await bad.models.list()


# -- LiteLLM -----------------------------------------------------------------------------------


async def test_litellm_with_mia_base_url(world: OAuthWorld) -> None:
    import litellm

    token = await access(world)
    world.script(answer("Через ", "LiteLLM"), answer("Поток"))
    response: Any = await litellm.acompletion(
        model="openai/mia:default",
        api_base=f"{ISSUER}/v1",
        api_key=token,
        messages=[{"role": "user", "content": "Привет"}],
        client=sdk(world, token),
    )
    assert response.choices[0].message.content == "Через LiteLLM"
    stream: Any = await litellm.acompletion(
        model="openai/mia:default",
        api_base=f"{ISSUER}/v1",
        api_key=token,
        messages=[{"role": "user", "content": "Ещё"}],
        stream=True,
        client=sdk(world, token),
    )
    text = ""
    async for chunk in stream:
        text += chunk.choices[0].delta.content or ""
    assert text == "Поток"
    assert world.gateway.requests[0].messages[-1].content == "Привет"


# -- memory.context: injected for the model, never returned raw --------------------------------


async def remember(world: OAuthWorld) -> None:
    user = str(world.user)
    await world.memory.create_fact(user_id=user, text="Работаю в студии «Сова»", category="work")
    await world.memory.create_fact(user_id=user, text="Люблю горький шоколад", category="profile")


async def test_memory_context_reaches_the_model_but_not_the_app(world: OAuthWorld) -> None:
    await remember(world)
    client = sdk(world, await access(world, scope="llm.generate memory.context"))
    world.script(answer("Готово"))
    response = await client.chat.completions.create(
        model="mia:default", messages=[{"role": "user", "content": "Где я работаю? студии"}]
    )
    system = world.gateway.requests[0].messages[0]
    assert system.role == "system" and "студии «Сова»" in system.content
    assert "горький шоколад" in system.content  # pinned profile fact
    body = response.model_dump_json()
    assert "Сова" not in body and "шоколад" not in body
    async with world.factory() as session:
        logged = (
            await session.scalars(select(MemoryAccessLog).where(MemoryAccessLog.actor != "user"))
        ).all()
    assert [(r.actor, r.action, r.app_client_id) for r in logged] == [
        ("app", "context", "https://notes.example/oauth/client.json")
    ]
    async with world.http() as http:
        log = await http.get(
            f"/v1/apps/{await grant_id(world)}/access-log", headers={"x-test-user": str(world.user)}
        )
    assert log.json()["items"][0]["actor"] == "app"


async def test_without_memory_context_nothing_is_injected(world: OAuthWorld) -> None:
    await remember(world)
    client = sdk(world, await access(world, scope="llm.generate memory.read:work"))
    world.script(answer("Нет"))
    await client.chat.completions.create(
        model="mia:default", messages=[{"role": "user", "content": "Где я работаю? студии"}]
    )
    assert [m.role for m in world.gateway.requests[0].messages] == ["user"]


async def test_context_follows_the_chosen_categories_and_the_opt_out(world: OAuthWorld) -> None:
    await remember(world)
    token = await access(
        world,
        scope="llm.generate memory.context",
        decision={"decision": "approve", "memory_categories": ["work"]},
    )
    client = sdk(world, token)
    world.script(answer("1"), answer("2"))
    await client.chat.completions.create(
        model="mia:default", messages=[{"role": "user", "content": "студии шоколад"}]
    )
    system = world.gateway.requests[0].messages[0].content
    assert "Сова" in system and "шоколад" not in system
    await client.chat.completions.create(
        model="mia:default",
        messages=[{"role": "user", "content": "студии"}],
        extra_body={"mia": {"context": False}},
    )
    assert [m.role for m in world.gateway.requests[1].messages] == ["user"]


# -- the monthly spending limit ----------------------------------------------------------------


async def test_monthly_limit_stops_the_app(world: OAuthWorld) -> None:
    client = sdk(
        world,
        await access(world, decision={"decision": "approve", "monthly_limit_usd": "0.03"}),
    )
    world.script(answer("Раз", cost=0.02), answer("Два", cost=0.02), answer("Три"))
    first = await client.chat.completions.create(
        model="mia:default", messages=[{"role": "user", "content": "1"}]
    )
    second = await client.chat.completions.create(
        model="mia:default", messages=[{"role": "user", "content": "2"}]
    )
    with pytest.raises(openai.RateLimitError) as refused:
        await client.chat.completions.create(
            model="mia:default", messages=[{"role": "user", "content": "3"}]
        )
    assert first.choices[0].message.content == "Раз" and second.choices[0].message.content == "Два"
    assert refused.value.code == "app_budget_exceeded"
    assert refused.value.response.headers["x-should-retry"] == "false"
    assert len(world.gateway.requests) == 2
    with pytest.raises(openai.RateLimitError):  # streaming is refused before any SSE
        await client.chat.completions.create(
            model="mia:default", messages=[{"role": "user", "content": "3"}], stream=True
        )
    async with world.http() as http:
        headers = {"x-test-user": str(world.user)}
        app = await grant_id(world)
        usage = (await http.get(f"/v1/apps/{app}/usage", headers=headers)).json()
        raised = await http.patch(
            f"/v1/apps/{app}",
            json={"monthly_limit_usd": "1.00"},
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
        )
    assert usage["month_spend_usd"] == "0.040000" and usage["monthly_limit_usd"] == "0.03"
    assert [i["cost_usd"] for i in usage["items"]] == ["0.020000", "0.020000"]
    assert raised.json()["monthly_limit_usd"] == "1.00"
    third = await client.chat.completions.create(
        model="mia:default", messages=[{"role": "user", "content": "3"}]
    )
    assert third.choices[0].message.content == "Три"


async def test_spend_without_a_provider_price_is_estimated(world: OAuthWorld) -> None:
    client = sdk(world, await access(world))
    world.script(answer("ok", tokens=(1_000_000, 500_000)))
    response = await client.chat.completions.create(
        model="mia:default", messages=[{"role": "user", "content": "x"}]
    )
    extra: Any = response.model_extra
    assert extra["mia"]["cost_usd"] == "1.500000" and extra["mia"]["cost_estimated"] is True
    async with world.http() as http:
        usage = await http.get(
            f"/v1/apps/{await grant_id(world)}/usage", headers={"x-test-user": str(world.user)}
        )
    assert usage.json()["items"][0]["estimated"] is True


# -- Mia tools (tools.use) ---------------------------------------------------------------------


@pytest.fixture
async def tools_world(tmp_path: Path) -> AsyncIterator[tuple[OAuthWorld, Any]]:
    holder: dict[str, Any] = {}

    async def router(users: list[uuid.UUID]) -> Any:
        holder["world"] = await build_world(tmp_path, user_id=str(users[0]), call_timeout_s=5)
        return holder["world"].router

    async with oauth_world(router_factory=router) as built:
        yield built, holder["world"]


async def test_mia_tools_run_inside_mia_for_granted_plugins(
    tools_world: tuple[OAuthWorld, Any],
) -> None:
    world, plugins = tools_world
    client = sdk(world, await access(world, scope="llm.generate tools.use:test.timer"))
    world.script(calls("test.timer.get", {}), answer("Таймеров нет."))
    response = await client.chat.completions.create(
        model="mia:default",
        messages=[{"role": "user", "content": "Какие таймеры?"}],
        extra_body={"mia": {"tools": ["test.timer"]}},
    )
    assert response.choices[0].message.content == "Таймеров нет."
    assert plugins.recorder.names() == ["get"]
    offered = {t.name for t in world.gateway.requests[0].tools}
    assert "test.timer.get" in offered and all(n.startswith("test.timer.") for n in offered)
    tool_message = world.gateway.requests[1].messages[-1]
    assert tool_message.role == "tool" and "No timers." in tool_message.content


async def test_mia_tools_need_the_scope_and_the_request(
    tools_world: tuple[OAuthWorld, Any],
) -> None:
    world, _ = tools_world
    client = sdk(world, await access(world))
    with pytest.raises(openai.PermissionDeniedError):
        await client.chat.completions.create(
            model="mia:default",
            messages=[{"role": "user", "content": "x"}],
            extra_body={"mia": {"tools": ["test.timer"]}},
        )
    scoped = sdk(world, await access(world, scope="llm.generate tools.use:test.timer"))
    world.script(answer("без инструментов"))
    await scoped.chat.completions.create(
        model="mia:default", messages=[{"role": "user", "content": "x"}]
    )
    assert world.gateway.requests[0].tools == ()


async def test_request_budget_stops_a_tool_loop(tools_world: tuple[OAuthWorld, Any]) -> None:
    world, plugins = tools_world
    client = sdk(world, await access(world, scope="llm.generate tools.use:test.timer"))
    world.script(calls("test.timer.get", {}, cost=0.02), answer("never"))
    response = await client.chat.completions.create(
        model="mia:default",
        messages=[{"role": "user", "content": "x"}],
        extra_body={"mia": {"tools": ["*"], "budget": {"max_usd": 0.01}}},
    )
    extra: Any = response.model_extra
    assert response.choices[0].finish_reason == "length"
    assert extra["mia"]["budget_exceeded"] is True
    assert len(world.gateway.requests) == 1
