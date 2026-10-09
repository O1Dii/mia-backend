"""ModelGateway behaviour with real connections: resolution, pause, fail-over, cancel."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.core.ports import ModelGateway
from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    TextDelta,
    ToolCallRequested,
)
from app.core.ports.types import DEFAULT_MODEL
from app.modules.models.chatgpt_plan import oauth
from app.modules.models.composition import ModelsStack
from app.modules.models.models import ModelConnection
from sqlalchemy import update

from tests.models.auth_world import CLIENT_ID
from tests.models.conftest import SessionFactory
from tests.models.helpers import TOOLS, assistant_with, make_request
from tests.models.replay import ReplayTransport

MakeUser = Callable[[], Awaitable[uuid.UUID]]
MakeStack = Callable[..., ModelsStack]
GLM = "zai-org/GLM-5.3-Flash"
GLM_REF = f"deepinfra/{GLM}"
PLAN_REF = "chatgpt-plan/gpt-6.1-sol"


def request_for(
    user: uuid.UUID, *messages: ModelMessage, model: str = DEFAULT_MODEL, tools: bool = False
) -> ModelRequest:
    req = make_request(*messages, tools=TOOLS if tools else ())
    return ModelRequest(
        user_id=str(user),
        profile=req.profile,
        messages=req.messages,
        tools=req.tools,
        model=model,
    )


async def add_plan(stack: ModelsStack, user: uuid.UUID) -> uuid.UUID:
    tokens = oauth.TokenSet(CLIENT_ID, "access-1", "refresh-1", time.time() + 36_000, [])
    return await stack.plan_store.save_sign_in(user, tokens, model="gpt-6.1-sol")


async def add_glm(stack: ModelsStack, user: uuid.UUID, *, primary: bool | None = None) -> uuid.UUID:
    view = await stack.connections.create(
        user,
        provider="deepinfra",
        label="GLM",
        model=GLM,
        api_key="di-key-0123456789abcdef",
        make_primary=primary,
    )
    return view.id


async def drain(stack: ModelsStack, req: ModelRequest) -> list[ModelEvent]:
    return [event async for event in stack.gateway.stream(req)]


async def test_gateway_satisfies_the_port_and_describes_the_primary_model(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    stack = make_stack(ReplayTransport())
    user = await make_user()
    assert isinstance(stack.gateway, ModelGateway)

    with pytest.raises(ModelGatewayError) as empty:
        await stack.gateway.describe(user_id=str(user))
    assert empty.value.code == "model_not_configured"

    await add_glm(stack, user)  # created first, but not primary-by-plan
    await add_plan(stack, user)
    info = await stack.gateway.describe(user_id=str(user))
    assert (info.ref, info.provider) == (PLAN_REF, "chatgpt-plan")
    assert info.supports_tools and info.verified and not info.supports_structured_output
    fallback = await stack.gateway.describe(user_id=str(user), model=GLM_REF)
    assert fallback.provider == "deepinfra" and fallback.verified is False
    with pytest.raises(ModelGatewayError) as unknown:
        await stack.gateway.describe(user_id=str(user), model="openai/gpt-x")
    assert unknown.value.code == "model_not_configured"
    with pytest.raises(ModelGatewayError) as malformed:
        await stack.gateway.describe(user_id=str(user), model="nonsense")
    assert malformed.value.code == "invalid_request"
    await stack.aclose()


async def test_default_model_answers_on_the_primary_connection(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    transport = ReplayTransport("plan_text")
    stack = make_stack(transport)
    user = await make_user()
    await add_plan(stack, user)
    events = await drain(stack, request_for(user, ModelMessage("user", "hi")))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello, ", "world!"]
    assert transport.bodies[0]["model"] == "gpt-6.1-sol"
    assert transport.requests[0].headers["authorization"] == "Bearer access-1"
    await stack.aclose()


async def test_connections_are_private_to_their_owner(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    stack = make_stack(ReplayTransport("plan_text"))
    owner, stranger = await make_user(), await make_user()
    await add_plan(stack, owner)
    with pytest.raises(ModelGatewayError) as caught:
        await drain(stack, request_for(stranger, ModelMessage("user", "hi")))
    assert caught.value.code == "model_not_configured"
    await stack.aclose()


async def test_usage_limit_pauses_the_plan_and_the_step_fails_over_to_glm(
    make_stack: MakeStack, make_user: MakeUser, session_factory: SessionFactory
) -> None:
    transport = ReplayTransport("plan_usage_limit_http", "glm_text", "glm_text")
    stack = make_stack(transport)
    user = await make_user()
    plan_id = await add_plan(stack, user)
    await add_glm(stack, user)

    events = await drain(stack, request_for(user, ModelMessage("user", "hi")))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello, ", "world!"]
    assert transport.requests[1].url.host == "api.deepinfra.com"

    view = await stack.connections.get(user, plan_id)
    assert view.status == "paused" and view.paused_until is not None
    assert timedelta(seconds=100) < view.paused_until - datetime.now(UTC) < timedelta(seconds=130)
    assert (await stack.gateway.describe(user_id=str(user))).provider == "deepinfra"

    # the next step does not even try the paused plan
    await drain(stack, request_for(user, ModelMessage("user", "again")))
    assert [r.url.host for r in transport.requests] == [
        "api.openai.com",
        "api.deepinfra.com",
        "api.deepinfra.com",
    ]

    # once the pause elapses the primary is used again
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ModelConnection)
            .where(ModelConnection.id == plan_id)
            .values(paused_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (await stack.gateway.describe(user_id=str(user))).provider == "chatgpt-plan"
    await stack.aclose()


async def test_usage_limit_inside_the_stream_before_any_text_also_fails_over(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    transport = ReplayTransport("plan_usage_limit_stream", "glm_text")
    stack = make_stack(transport)
    user = await make_user()
    plan_id = await add_plan(stack, user)
    await add_glm(stack, user)
    events = await drain(stack, request_for(user, ModelMessage("user", "hi")))
    assert events[-1] == ModelDone("stop")
    assert (await stack.connections.get(user, plan_id)).status == "paused"
    await stack.aclose()


async def test_no_silent_switch_after_output_started_or_for_explicit_models(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    transport = ReplayTransport("plan_failed_midstream", "plan_usage_limit_http", "glm_text")
    stack = make_stack(transport)
    user = await make_user()
    plan_id = await add_plan(stack, user)
    await add_glm(stack, user)

    seen: list[object] = []
    with pytest.raises(ModelGatewayError) as midstream:
        async for event in stack.gateway.stream(request_for(user, ModelMessage("user", "hi"))):
            seen.append(event)
    assert seen == [TextDelta("partial")] and midstream.value.code == "provider_unavailable"
    assert (await stack.connections.get(user, plan_id)).status == "active"  # not a usage limit
    assert transport.remaining == 2

    with pytest.raises(ModelGatewayError) as explicit:
        await drain(stack, request_for(user, ModelMessage("user", "hi"), model=PLAN_REF))
    assert explicit.value.code == "rate_limited"
    assert (await stack.connections.get(user, plan_id)).status == "paused"
    assert transport.remaining == 1  # GLM was never consulted
    await stack.aclose()


async def test_everything_paused_reports_rate_limited_not_unconfigured(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    stack = make_stack(ReplayTransport("plan_usage_limit_http"))
    user = await make_user()
    await add_plan(stack, user)
    with pytest.raises(ModelGatewayError):
        await drain(stack, request_for(user, ModelMessage("user", "hi")))
    with pytest.raises(ModelGatewayError) as paused:
        await drain(stack, request_for(user, ModelMessage("user", "hi")))
    assert paused.value.code == "rate_limited" and paused.value.retryable
    assert (await stack.gateway.describe(user_id=str(user))).provider == "chatgpt-plan"
    await stack.aclose()


class DeadRefreshTransport(ReplayTransport):
    """API calls replay from cassettes; the token endpoint says the refresh token is dead."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.openai.com":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return await super().handle_async_request(request)


async def test_rejected_credentials_mark_reauth_and_fail_over(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    transport = DeadRefreshTransport("plan_401_single", "glm_text")
    stack = make_stack(transport)
    user = await make_user()
    plan_id = await add_plan(stack, user)
    await add_glm(stack, user)
    events = await drain(stack, request_for(user, ModelMessage("user", "hi")))
    assert events[-1] == ModelDone("stop")
    assert (await stack.connections.get(user, plan_id)).status == "needs_reauth"
    await stack.aclose()


async def test_tool_loop_through_the_gateway_preserves_provider_state(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    transport = ReplayTransport("glm_tool_loop")
    stack = make_stack(transport)
    user = await make_user()
    await add_glm(stack, user)
    messages = [ModelMessage("user", "Call Anna")]
    for _ in range(3):
        events = await drain(stack, request_for(user, *messages, tools=True))
        calls = [e.call for e in events if isinstance(e, ToolCallRequested)]
        if not calls:
            break
        messages += [
            assistant_with(calls[0]),
            ModelMessage("tool", "{}", tool_call_id=calls[0].call_id),
        ]
    assert transport.remaining == 0
    assert (
        transport.bodies[1]["messages"][1]["reasoning_content"] == "I should call get_time first."
    )
    await stack.aclose()


async def test_cancelling_the_step_closes_the_provider_connection(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    transport = ReplayTransport("plan_text")
    stack = make_stack(transport)
    user = await make_user()
    await add_plan(stack, user)
    stream = stack.gateway.stream(request_for(user, ModelMessage("user", "hi")))
    assert isinstance(await anext(stream), TextDelta)
    await stream.aclose()  # type: ignore[attr-defined]
    assert transport.streams[0].closed
    await stack.aclose()


async def test_chat_only_connections_reject_tools_and_never_serve_the_default_model(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    stack = make_stack(ReplayTransport())
    user = await make_user()
    await stack.connections.create(
        user, provider="ollama", label="local", model="llama", api_key=None
    )
    with pytest.raises(ModelGatewayError) as default:
        await drain(stack, request_for(user, ModelMessage("user", "hi")))
    assert default.value.code == "model_not_configured"
    with pytest.raises(ModelGatewayError) as explicit:
        await drain(
            stack, request_for(user, ModelMessage("user", "hi"), model="ollama/llama", tools=True)
        )
    assert explicit.value.code == "invalid_request"
    info = await stack.gateway.describe(user_id=str(user), model="ollama/llama")
    assert info.supports_tools is False
    await stack.aclose()


async def test_probe_records_the_result_and_resumes_a_paused_connection(
    make_stack: MakeStack, make_user: MakeUser
) -> None:
    transport = ReplayTransport("glm_rate_limited", "glm_text")
    stack = make_stack(transport)
    user = await make_user()
    connection = await add_glm(stack, user)

    failed = await stack.gateway.probe(user, connection)
    assert (failed.ok, failed.error_code) == (False, "rate_limited")
    stored = await stack.connections.get(user, connection)
    assert stored.last_test_ok is False and stored.last_test_error_code == "rate_limited"

    ok = await stack.gateway.probe(user, connection)
    assert ok.ok and ok.error_code is None and ok.latency_ms >= 0
    body = transport.bodies[1]
    assert body["max_tokens"] == 16 and "tools" not in body
    assert (await stack.connections.get(user, connection)).last_test_ok is True
    await stack.aclose()
