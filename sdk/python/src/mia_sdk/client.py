"""Thin async client of the Mia API for apps (paths from the generated operation table).

``chat_completions``/``responses`` are OpenAI-compatible; :meth:`MiaClient.openai` returns
an ``openai.AsyncOpenAI`` pointed at Mia when the ``openai`` package is installed. Runs
(``create_run``/``run_events``) use first-party tokens.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from mia_sdk._operations import OPERATIONS


class MiaApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _path(operation_id: str, params: Mapping[str, str] | None = None) -> tuple[str, str]:
    operation = OPERATIONS[operation_id]
    path = operation["path"]
    for name, value in (params or {}).items():
        path = path.replace("{" + name + "}", str(value))
    return operation["method"], path


async def _sse(response: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    event: str | None = None
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield event, "\n".join(data)


class MiaClient:
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        http: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self._http = http or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> MiaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _headers(self, operation_id: str, idempotency_key: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if OPERATIONS[operation_id]["idempotency_key"]:
            headers["Idempotency-Key"] = idempotency_key or uuid.uuid4().hex
        return headers

    async def call(
        self,
        operation_id: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
        query: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        method, path = _path(operation_id, params)
        response = await self._http.request(
            method,
            self.base_url + path,
            json=body,
            params=query,
            headers=self._headers(operation_id, idempotency_key),
        )
        if response.status_code >= 400:
            raise _error(response)
        return None if response.status_code == 204 else response.json()

    async def _stream(
        self, operation_id: str, body: Mapping[str, Any]
    ) -> AsyncIterator[tuple[str | None, str]]:
        method, path = _path(operation_id)
        async with self._http.stream(
            method,
            self.base_url + path,
            json=dict(body),
            headers=self._headers(operation_id, None),
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise _error(response)
            async for item in _sse(response):
                yield item

    # -- OpenAI-compatible -------------------------------------------------------------

    async def chat_completions(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return dict(await self.call("createChatCompletion", body={**body, "stream": False}))

    async def stream_chat_completions(
        self, body: Mapping[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        async for _, data in self._stream("createChatCompletion", {**body, "stream": True}):
            if data == "[DONE]":
                return
            chunk = json.loads(data)
            if "error" in chunk:
                error = chunk["error"]
                raise MiaApiError(0, str(error.get("code")), str(error.get("message")))
            yield chunk

    async def responses(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return dict(await self.call("createResponse", body={**body, "stream": False}))

    async def models(self) -> list[str]:
        listed = await self.call("listAppModels")
        return [str(item["id"]) for item in listed["data"]]

    def openai(self) -> Any:
        """``openai.AsyncOpenAI`` with ``base_url`` = Mia (requires the ``openai`` package)."""
        import openai

        return openai.AsyncOpenAI(base_url=f"{self.base_url}/v1", api_key=self.access_token)

    # -- runs (first-party tokens) --------------------------------------------------------

    async def create_run(
        self, text: str, *, profile: str = "chat", idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return dict(
            await self.call(
                "createRun",
                body={"profile": profile, "input": {"text": text}},
                idempotency_key=idempotency_key,
            )
        )

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        return dict(await self.call("cancelRun", params={"run_id": run_id}))

    async def run_events(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        method, path = _path("streamRunEvents", {"run_id": run_id})
        async with self._http.stream(
            method, self.base_url + path, headers=self._headers("streamRunEvents", None)
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise _error(response)
            async for event, data in _sse(response):
                item = json.loads(data)
                yield item
                if event in ("run.completed", "run.failed", "run.cancelled"):
                    return


def _error(response: httpx.Response) -> MiaApiError:
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    return MiaApiError(
        response.status_code, str(error.get("code", "unknown")), str(error.get("message", ""))
    )
