"""``ModelGateway`` implementation: resolve ``mia:default``, run adapters, fail over.

Resolution (per user): ``mia:default`` -> the primary connection, then the remaining
tools-capable connections as an ordered fallback chain; ``<provider>/<model>`` -> exactly
that connection. A *pause* signal (``subscription_sharing_usage_limit_exceeded``) or a
rejected credential takes the connection out of rotation; when nothing has been streamed
yet and the caller asked for ``mia:default``, the next candidate serves the step. A stream
that already emitted events is never silently continued on another provider.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

import httpx

from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelGatewayError,
    ModelInfo,
    ModelMessage,
    ModelRequest,
    TextDelta,
)
from app.core.ports.types import DEFAULT_MODEL
from app.modules.models.adapters import AdapterSettings, AdapterTarget, TokenSource, build_adapter
from app.modules.models.chatgpt_plan.credentials import PlanCredentialStore
from app.modules.models.connections import ConnectionService, ResolvedConnection
from app.modules.models.errors import ProviderError

logger = logging.getLogger(__name__)

PROBE_PROMPT = "Reply with the single word OK."
PROBE_MAX_TOKENS = 16


class DefaultModelGateway:
    def __init__(
        self,
        connections: ConnectionService,
        client: httpx.AsyncClient,
        settings: AdapterSettings,
        *,
        plan_store: PlanCredentialStore | None = None,
    ) -> None:
        self._connections = connections
        self._client = client
        self._settings = settings
        self._plan_store = plan_store

    # ------------------------------------------------------------ describe

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        uid = _uuid(user_id)
        candidates = await self._connections.resolve(uid, model, include_unavailable=True)
        if not candidates:
            raise ModelGatewayError(
                "model_not_configured", "No model connection is configured for this user."
            )
        usable = await self._connections.resolve(uid, model)
        return _info(usable[0] if usable else candidates[0])

    # -------------------------------------------------------------- stream

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        uid = _uuid(request.user_id)
        candidates = await self._connections.resolve(uid, request.model)
        if not candidates:
            every = await self._connections.resolve(uid, request.model, include_unavailable=True)
            if every and any(c.status == "paused" for c in every):
                raise ProviderError("rate_limited", retryable=True)
            raise ModelGatewayError(
                "model_not_configured", "No usable model connection is configured."
            )
        can_fail_over = request.model == DEFAULT_MODEL
        last: ModelGatewayError | None = None
        for index, candidate in enumerate(candidates):
            emitted = False
            try:
                async with aclosing(self._run(candidate, request)) as events:
                    async for event in events:
                        emitted = True
                        yield event
                return
            except ProviderError as error:
                await self._note_failure(candidate, error)
                last = error
                more = index + 1 < len(candidates)
                if emitted or not (can_fail_over and more and error.switchable):
                    raise
                logger.warning(
                    "model_failover",
                    extra={"provider": candidate.spec.id.replace("-", "_")},
                )
        if last is not None:  # pragma: no cover - the loop always returns or raises
            raise last

    async def _run(
        self, candidate: ResolvedConnection, request: ModelRequest
    ) -> AsyncGenerator[ModelEvent, None]:
        if request.tools and candidate.capability != "tools":
            raise ProviderError(
                "invalid_request", "This model connection is chat-only and cannot use tools."
            )
        target = AdapterTarget(
            spec=candidate.spec,
            model=candidate.model,
            base_url=candidate.base_url,
            api_key=candidate.api_key,
            tokens=self._tokens_for(candidate),
        )
        adapter = build_adapter(self._client, target, self._settings)
        finished = False
        async with aclosing(adapter.stream(request)) as events:
            async for event in events:
                finished = finished or isinstance(event, ModelDone)
                yield event
        if not finished:
            raise ProviderError(
                "provider_unavailable", "The model stream ended early.", retryable=True
            )

    def _tokens_for(self, candidate: ResolvedConnection) -> TokenSource | None:
        if candidate.spec.id != "chatgpt-plan":
            return None
        if self._plan_store is None:
            raise ProviderError("model_not_configured", "ChatGPT sign-in is not enabled.")
        return self._plan_store.token_source(candidate.user_id, candidate.id)

    async def _note_failure(self, candidate: ResolvedConnection, error: ProviderError) -> None:
        if error.usage_limit:
            await self._connections.pause(
                candidate.id,
                seconds=error.pause_seconds or self._settings.pause_seconds,
                reason=error.provider_code or "usage_limit",
            )
        elif error.needs_reauth:
            await self._connections.mark_needs_reauth(candidate.id)

    # --------------------------------------------------------------- probe

    async def probe(self, user_id: uuid.UUID, connection_id: uuid.UUID) -> ProbeResult:
        """ "Test API": one tiny real request against exactly this connection."""
        candidate = await self._connections.resolve_one(user_id, connection_id)
        request = ModelRequest(
            user_id=str(user_id),
            profile="chat",
            messages=[ModelMessage("user", PROBE_PROMPT)],
            model=f"{candidate.spec.id}/{candidate.model}",
            structured_response=False,
            max_output_tokens=PROBE_MAX_TOKENS,
        )
        started = time.perf_counter()
        ok, code = True, None
        try:
            got_text = False
            async with aclosing(self._run(candidate, request)) as events:
                async for event in events:
                    got_text = got_text or (isinstance(event, TextDelta) and bool(event.text))
            if not got_text:
                ok, code = False, "empty_answer"
        except ProviderError as error:
            await self._note_failure(candidate, error)
            ok, code = False, error.code
        except ModelGatewayError as error:
            ok, code = False, error.code
        latency_ms = int((time.perf_counter() - started) * 1000)
        await self._connections.record_test(connection_id, ok=ok, error_code=code)
        return ProbeResult(ok=ok, latency_ms=latency_ms, error_code=code)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    latency_ms: int
    error_code: str | None


def _uuid(user_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(user_id)
    except ValueError:
        raise ModelGatewayError("invalid_request", "user_id must be a UUID.") from None


def _info(candidate: ResolvedConnection) -> ModelInfo:
    tools = candidate.capability == "tools"
    return ModelInfo(
        ref=f"{candidate.spec.id}/{candidate.model}",
        provider=candidate.spec.id,
        supports_tools=tools,
        supports_structured_output=False,
        verified=candidate.spec.verified and tools,
    )
