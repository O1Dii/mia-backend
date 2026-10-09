"""OpenAI-compatible app endpoints: ``/v1/chat/completions``, ``/v1/responses``, ``/v1/models``.

Any app on the OpenAI SDK or LiteLLM works through Mia by changing ``base_url`` and the
token (Review §8.2). ``model`` is always ``mia:default`` — the user's active model — and
the request runs as ``Run(app)`` (:mod:`app.modules.oauth.apprun`). Mia extensions go in
the ``mia`` body field (``extra_body={"mia": {...}}`` in the SDKs):

- ``context`` (bool, default true): inject memory when the token has ``memory.context``;
- ``tools`` (list of plugin ids or ``["*"]``): offer the user's installed plugins covered
  by ``tools.use[:<plugin>]``; Mia executes them, approvals work as usual;
- ``budget.max_usd`` (number): a per-request spending cap below the monthly limit.

Requires scope ``llm.generate``. Exempt from ``Idempotency-Key`` (``x-mia-idempotency``).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.ports.model_gateway import ModelMessage, ModelToolCall, ToolSpec
from app.core.ports.types import DEFAULT_MODEL
from app.modules.oauth.apprun import AppRequest, AppResult, AppRunner, tool_arguments_json
from app.modules.oauth.errors import AppApiError, app_error
from app.modules.oauth.principal import AppAuthenticator
from app.modules.oauth.service import AppAccess
from app.modules.oauth.usage import Surface
from app.modules.runs.errors import public_error

MAX_MESSAGES = 256
MAX_TOTAL_CHARS = 400_000
MAX_TOOLS = 64
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_RUN_STATUS = {
    "model_not_configured": 409,
    "invalid_request": 400,
    "context_too_long": 400,
    "rate_limited": 429,
    "provider_unavailable": 503,
    "provider_timeout": 503,
    "app_run_timeout": 504,
    "app_run_cancelled": 409,
}
_APP_RUN_ERRORS = {
    "app_run_timeout": ("The request did not finish in time.", True),
    "app_run_cancelled": ("The request was cancelled by the user.", False),
}


def _invalid(message: str) -> AppApiError:
    return AppApiError(400, "validation_error", message)


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                raise app_error("unsupported_content")
            kind = part.get("type")
            if kind in ("text", "input_text", "output_text") and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif kind == "refusal" and isinstance(part.get("refusal"), str):
                parts.append(part["refusal"])
            else:
                raise app_error("unsupported_content")
        return "".join(parts)
    raise app_error("unsupported_content")


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise _invalid("Tool call arguments must be a JSON string.")
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise _invalid("Tool call arguments must be valid JSON.") from error
    if not isinstance(value, dict):
        raise _invalid("Tool call arguments must be a JSON object.")
    return value


def _tool_name(name: Any) -> str:
    if not isinstance(name, str) or not name or len(name) > 64:
        raise _invalid("Invalid tool name.")
    if not all(c.isalnum() or c in "_-" for c in name):
        raise _invalid("Tool names may contain letters, digits, '_' and '-' only.")
    return name


def _function_spec(name: Any, description: Any, parameters: Any) -> ToolSpec:
    schema = parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}
    return ToolSpec(
        name=_tool_name(name),
        description=description if isinstance(description, str) else "",
        input_schema=schema,
    )


def _chat_messages(raw: Any) -> tuple[ModelMessage, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_MESSAGES:
        raise _invalid("messages must be a non-empty list.")
    messages: list[ModelMessage] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("Each message must be an object.")
        role = item.get("role")
        if role in ("system", "developer"):
            messages.append(ModelMessage(role="system", content=_text(item.get("content"))))
        elif role == "user":
            messages.append(ModelMessage(role="user", content=_text(item.get("content"))))
        elif role == "assistant":
            calls: list[ModelToolCall] = []
            for call in item.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict) or not isinstance(call.get("id"), str):
                    raise _invalid("Invalid assistant tool_calls.")
                calls.append(
                    ModelToolCall(
                        call_id=call["id"],
                        name=str(function.get("name", "")),
                        arguments=_arguments(function.get("arguments")),
                    )
                )
            messages.append(
                ModelMessage(
                    role="assistant", content=_text(item.get("content")), tool_calls=tuple(calls)
                )
            )
        elif role == "tool":
            call_id = item.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise _invalid("tool messages need tool_call_id.")
            messages.append(
                ModelMessage(role="tool", content=_text(item.get("content")), tool_call_id=call_id)
            )
        else:
            raise _invalid("Unsupported message role.")
    if sum(len(m.content) for m in messages) > MAX_TOTAL_CHARS:
        raise _invalid("The conversation is too long.")
    return tuple(messages)


def _responses_input(raw: Any, instructions: Any) -> tuple[ModelMessage, ...]:
    messages: list[ModelMessage] = []
    if isinstance(instructions, str) and instructions:
        messages.append(ModelMessage(role="system", content=instructions))
    if isinstance(raw, str):
        messages.append(ModelMessage(role="user", content=raw))
    elif isinstance(raw, list) and 0 < len(raw) <= MAX_MESSAGES:
        for item in raw:
            if not isinstance(item, dict):
                raise _invalid("Each input item must be an object.")
            kind = item.get("type", "message")
            if kind == "message":
                role = item.get("role")
                mapped = {"developer": "system", "system": "system", "user": "user"}.get(
                    str(role), "assistant" if role == "assistant" else None
                )
                if mapped is None:
                    raise _invalid("Unsupported input role.")
                messages.append(
                    ModelMessage(role=mapped, content=_text(item.get("content")))  # type: ignore[arg-type]
                )
            elif kind == "function_call":
                call = ModelToolCall(
                    call_id=str(item.get("call_id", "")),
                    name=str(item.get("name", "")),
                    arguments=_arguments(item.get("arguments")),
                )
                messages.append(ModelMessage(role="assistant", content="", tool_calls=(call,)))
            elif kind == "function_call_output":
                output = item.get("output")
                messages.append(
                    ModelMessage(
                        role="tool",
                        content=output if isinstance(output, str) else json.dumps(output),
                        tool_call_id=str(item.get("call_id", "")),
                    )
                )
            else:
                raise app_error("unsupported_content")
    else:
        raise _invalid("input must be a string or a non-empty list.")
    if sum(len(m.content) for m in messages) > MAX_TOTAL_CHARS:
        raise _invalid("The conversation is too long.")
    return tuple(messages)


def _extensions(body: dict[str, Any], access: AppAccess) -> tuple[bool, tuple[str, ...], Any]:
    mia = body.get("mia") or {}
    if not isinstance(mia, dict):
        raise _invalid("mia must be an object.")
    context = mia.get("context", True)
    if not isinstance(context, bool):
        raise _invalid("mia.context must be a boolean.")
    tools = mia.get("tools") or []
    if tools is True:
        tools = ["*"]
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise _invalid("mia.tools must be a list of plugin ids.")
    if tools and not any(s == "tools.use" or s.startswith("tools.use:") for s in access.scopes):
        raise app_error("insufficient_scope")
    budget = mia.get("budget") or {}
    max_usd = None
    if budget:
        if not isinstance(budget, dict):
            raise _invalid("mia.budget must be an object.")
        try:
            max_usd = Decimal(str(budget.get("max_usd"))) if "max_usd" in budget else None
        except InvalidOperation as error:
            raise _invalid("mia.budget.max_usd must be a number.") from error
        if max_usd is not None and max_usd <= 0:
            raise _invalid("mia.budget.max_usd must be positive.")
    return context, tuple(tools[:32]), max_usd


def _check_model(body: dict[str, Any]) -> None:
    if body.get("model") != DEFAULT_MODEL:
        raise app_error("unsupported_model")
    n = body.get("n")
    if n not in (None, 1):
        raise _invalid("Only n=1 is supported.")


def _max_tokens(body: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = body.get(name)
        if value is not None:
            if not isinstance(value, int) or value < 1:
                raise _invalid(f"{name} must be a positive integer.")
            return value
    return None


def _run_error(request: Request, code: str) -> JSONResponse:
    if code in _APP_RUN_ERRORS:
        message, retryable = _APP_RUN_ERRORS[code]
    else:
        error = public_error(code)
        code, message, retryable = error["code"], error["message"], bool(error["retryable"])
    return JSONResponse(
        status_code=_RUN_STATUS.get(code, 500),
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": str(getattr(request.state, "request_id", "request")),
                "retryable": retryable,
            }
        },
    )


def _failed(result: AppResult) -> bool:
    return (
        result.error_code is not None
        and result.error_code != "app_budget_exceeded"
        and not result.text
        and not result.client_calls
    )


def _mia_body(result: AppResult) -> dict[str, Any]:
    body: dict[str, Any] = {
        "run_id": str(result.run_id),
        "cost_usd": str(result.cost_usd.quantize(Decimal("0.000001"))),
        "cost_estimated": result.estimated,
    }
    if result.error_code == "app_budget_exceeded":
        body["budget_exceeded"] = True
    return body


def _chat_tool_calls(calls: Sequence[ModelToolCall], *, stream: bool) -> list[dict[str, Any]]:
    out = []
    for index, call in enumerate(calls):
        item: dict[str, Any] = {
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": tool_arguments_json(call)},
        }
        if stream:
            item["index"] = index
        out.append(item)
    return out


def _sse(data: dict[str, Any] | str, *, event: str | None = None) -> bytes:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n".encode()


def build_openai_compat_router(*, runner: AppRunner, apps: AppAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["Apps"])

    async def parse(request: Request) -> tuple[AppAccess, dict[str, Any]]:
        access = await apps.authenticate(request, required=("llm.generate",))
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise _invalid("The body must be JSON.") from error
        if not isinstance(body, dict):
            raise _invalid("The body must be a JSON object.")
        _check_model(body)
        return access, body

    def app_request(
        access: AppAccess,
        body: dict[str, Any],
        messages: tuple[ModelMessage, ...],
        tools: tuple[ToolSpec, ...],
        surface: Surface,
        max_tokens: int | None,
    ) -> AppRequest:
        context, mia_tools, max_usd = _extensions(body, access)
        if body.get("tool_choice") == "none":
            tools = ()
        return AppRequest(
            access=access,
            messages=messages,
            surface=surface,
            client_tools=tools,
            mia_tools=mia_tools,
            context=context,
            max_cost_usd=max_usd,
            max_output_tokens=max_tokens,
        )

    @router.get("/models")
    async def list_models(request: Request) -> JSONResponse:
        await apps.authenticate(request, required=("llm.generate",))
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": DEFAULT_MODEL, "object": "model", "created": 0, "owned_by": "mia"}],
            }
        )

    # -- chat completions -----------------------------------------------------------

    @router.post("/chat/completions", openapi_extra={"x-mia-idempotency": "exempt"})
    async def chat_completions(request: Request) -> Any:
        access, body = await parse(request)
        raw_tools = body.get("tools") or []
        if not isinstance(raw_tools, list) or len(raw_tools) > MAX_TOOLS:
            raise _invalid("tools must be a list.")
        tools: list[ToolSpec] = []
        for tool in raw_tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if not isinstance(function, dict) or tool.get("type") != "function":
                raise _invalid("Only function tools are supported.")
            tools.append(
                _function_spec(
                    function.get("name"), function.get("description"), function.get("parameters")
                )
            )
        app_req = app_request(
            access,
            body,
            _chat_messages(body.get("messages")),
            tuple(tools),
            "chat_completions",
            _max_tokens(body, "max_completion_tokens", "max_tokens"),
        )
        created = int(time.time())
        if body.get("stream") is True:
            options = body.get("stream_options") or {}
            include_usage = isinstance(options, dict) and options.get("include_usage") is True
            items = await runner.stream(app_req)
            return StreamingResponse(
                _chat_stream(request, items, created, include_usage),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        result = await runner.complete(app_req)
        if _failed(result):
            return _run_error(request, str(result.error_code))
        message: dict[str, Any] = {"role": "assistant", "content": result.text or None}
        if result.client_calls:
            message["tool_calls"] = _chat_tool_calls(result.client_calls, stream=False)
        return JSONResponse(
            {
                "id": f"chatcmpl-{result.run_id.hex}",
                "object": "chat.completion",
                "created": created,
                "model": DEFAULT_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": result.finish_reason,
                        "logprobs": None,
                    }
                ],
                "usage": {
                    "prompt_tokens": result.input_tokens,
                    "completion_tokens": result.output_tokens,
                    "total_tokens": result.input_tokens + result.output_tokens,
                },
                "mia": _mia_body(result),
            }
        )

    async def _chat_stream(
        request: Request,
        items: AsyncIterator[Any],
        created: int,
        include_usage: bool,
    ) -> AsyncIterator[bytes]:
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"

        def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
            return _sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": DEFAULT_MODEL,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )

        yield chunk({"role": "assistant", "content": ""})
        async for kind, value in items:
            if kind == "delta":
                yield chunk({"content": value})
                continue
            result: AppResult = value
            if _failed(result):
                error = json.loads(_run_error(request, str(result.error_code)).body)
                yield _sse(error)
                yield _sse("[DONE]")
                return
            if result.client_calls:
                yield chunk({"tool_calls": _chat_tool_calls(result.client_calls, stream=True)})
            yield chunk({}, result.finish_reason)
            if include_usage:
                yield _sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": DEFAULT_MODEL,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": result.input_tokens,
                            "completion_tokens": result.output_tokens,
                            "total_tokens": result.input_tokens + result.output_tokens,
                        },
                        "mia": _mia_body(result),
                    }
                )
        yield _sse("[DONE]")

    # -- responses --------------------------------------------------------------------

    @router.post("/responses", openapi_extra={"x-mia-idempotency": "exempt"})
    async def responses(request: Request) -> Any:
        access, body = await parse(request)
        raw_tools = body.get("tools") or []
        if not isinstance(raw_tools, list) or len(raw_tools) > MAX_TOOLS:
            raise _invalid("tools must be a list.")
        tools = []
        for tool in raw_tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise _invalid("Only function tools are supported.")
            tools.append(
                _function_spec(tool.get("name"), tool.get("description"), tool.get("parameters"))
            )
        app_req = app_request(
            access,
            body,
            _responses_input(body.get("input"), body.get("instructions")),
            tuple(tools),
            "responses",
            _max_tokens(body, "max_output_tokens"),
        )
        created = int(time.time())
        if body.get("stream") is True:
            items = await runner.stream(app_req)
            return StreamingResponse(
                _responses_stream(request, items, created),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        result = await runner.complete(app_req)
        if _failed(result):
            return _run_error(request, str(result.error_code))
        return JSONResponse(_response_object(result, created, f"resp_{result.run_id.hex}"))

    async def _responses_stream(
        request: Request, items: AsyncIterator[Any], created: int
    ) -> AsyncIterator[bytes]:
        response_id = f"resp_{uuid.uuid4().hex}"
        message_id = f"msg_{uuid.uuid4().hex}"
        sequence = 0

        def event(kind: str, payload: dict[str, Any]) -> bytes:
            nonlocal sequence
            body = {"type": kind, "sequence_number": sequence, **payload}
            sequence += 1
            return _sse(body, event=kind)

        shell = _response_object(None, created, response_id, status="in_progress")
        yield event("response.created", {"response": shell})
        yield event("response.in_progress", {"response": shell})
        message_open = False
        text_parts: list[str] = []

        def open_message() -> list[bytes]:
            item = {
                "type": "message",
                "id": message_id,
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            part = {"type": "output_text", "text": "", "annotations": []}
            return [
                event("response.output_item.added", {"output_index": 0, "item": item}),
                event(
                    "response.content_part.added",
                    {"item_id": message_id, "output_index": 0, "content_index": 0, "part": part},
                ),
            ]

        async for kind, value in items:
            if kind == "delta":
                if not message_open:
                    for frame in open_message():
                        yield frame
                    message_open = True
                text_parts.append(value)
                yield event(
                    "response.output_text.delta",
                    {
                        "item_id": message_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": value,
                        "logprobs": [],
                    },
                )
                continue
            result: AppResult = value
            if _failed(result):
                error = json.loads(_run_error(request, str(result.error_code)).body)["error"]
                yield event(
                    "error",
                    {"code": error["code"], "message": error["message"], "param": None},
                )
                return
            final = _response_object(result, created, response_id, message_id=message_id)
            if message_open:
                text = "".join(text_parts)
                part = {"type": "output_text", "text": text, "annotations": []}
                yield event(
                    "response.output_text.done",
                    {
                        "item_id": message_id,
                        "output_index": 0,
                        "content_index": 0,
                        "text": text,
                        "logprobs": [],
                    },
                )
                yield event(
                    "response.content_part.done",
                    {"item_id": message_id, "output_index": 0, "content_index": 0, "part": part},
                )
                yield event(
                    "response.output_item.done", {"output_index": 0, "item": final["output"][0]}
                )
            offset = 1 if message_open else 0
            for index, item in enumerate(i for i in final["output"] if i["type"] != "message"):
                yield event(
                    "response.output_item.added", {"output_index": index + offset, "item": item}
                )
                yield event(
                    "response.output_item.done", {"output_index": index + offset, "item": item}
                )
            kind_done = (
                "response.completed" if final["status"] == "completed" else "response.incomplete"
            )
            yield event(kind_done, {"response": final})

    return router


def _response_object(
    result: AppResult | None,
    created: int,
    response_id: str,
    *,
    status: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    usage = None
    incomplete = None
    if result is not None:
        if result.text:
            output.append(
                {
                    "type": "message",
                    "id": message_id or f"msg_{result.run_id.hex}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": result.text, "annotations": []}],
                }
            )
        for call in result.client_calls:
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex}",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": tool_arguments_json(call),
                    "status": "completed",
                }
            )
        usage = {
            "input_tokens": result.input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": result.output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": result.input_tokens + result.output_tokens,
        }
        if result.finish_reason == "length":
            status = status or "incomplete"
            incomplete = {"reason": "max_output_tokens"}
    body: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status or "completed",
        "model": DEFAULT_MODEL,
        "output": output,
        "error": None,
        "incomplete_details": incomplete,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": usage,
    }
    if result is not None:
        body["mia"] = _mia_body(result)
    return body
