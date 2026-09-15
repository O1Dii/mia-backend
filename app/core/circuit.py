"""Process-local provider circuit breakers with one recovery probe."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid5

from app.core.metrics import METRICS
from app.providers.ai.base import (
    AIEvent,
    ProviderError,
    ResponseCompleted,
    TextAIProvider,
    TextRequest,
)

logger = logging.getLogger(__name__)


class ProviderCircuitOpenError(RuntimeError):
    """The provider circuit is open; no upstream call was attempted."""


class ProviderCircuit:
    """Closed -> open after ``threshold`` consecutive failures -> half-open single probe."""

    def __init__(
        self,
        threshold: int,
        cooldown_seconds: int,
        *,
        name: str = "provider",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.name = name
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self._probing = False
        METRICS.set("mia_provider_circuit_open", 0, {"provider": name})

    @property
    def state(self) -> str:
        if self._open_until == 0:
            return "closed"
        if self._clock() < self._open_until or self._probing:
            return "open"
        return "half_open"

    def available(self) -> bool:
        """Report whether a new call could be admitted, without claiming the probe."""
        return self.state != "open"

    def allow(self) -> bool:
        if self._open_until == 0:
            return True
        if self._clock() < self._open_until or self._probing:
            return False
        self._probing = True
        return True

    def success(self) -> None:
        was_open = self._open_until != 0
        self._failures = 0
        self._open_until = 0
        self._probing = False
        if was_open:
            METRICS.set("mia_provider_circuit_open", 0, {"provider": self.name})
            logger.info("provider_circuit_recovered", extra={"provider": self.name})

    def failure(self) -> None:
        self._failures += 1
        if self._probing or self._failures >= self.threshold:
            was_open = self._open_until != 0
            self._open_until = self._clock() + self.cooldown_seconds
            if not was_open:
                METRICS.set("mia_provider_circuit_open", 1, {"provider": self.name})
                METRICS.inc("mia_provider_circuit_opened_total", {"provider": self.name})
                logger.warning("provider_circuit_opened", extra={"provider": self.name})
        self._probing = False

    def abandon_probe(self) -> None:
        self._probing = False


class GuardedTextProvider:
    """Count only provider failures; never expose exception text to clients."""

    def __init__(self, provider: TextAIProvider, circuit: ProviderCircuit) -> None:
        self._provider = provider
        self._circuit = circuit

    async def stream(self, request: TextRequest) -> AsyncIterator[AIEvent]:
        probe = self._circuit.state == "half_open"
        if not self._circuit.allow():
            yield self._unavailable(request, seq=1)
            return
        terminal = False
        last_seq = 0
        try:
            async for event in self._provider.stream(request):
                last_seq = event.seq
                if isinstance(event, ProviderError):
                    self._circuit.failure()
                    terminal = True
                elif isinstance(event, ResponseCompleted):
                    self._circuit.success()
                    terminal = True
                yield event
            if not terminal:
                self._circuit.failure()
                terminal = True
        except Exception:
            self._circuit.failure()
            terminal = True
            yield self._unavailable(request, seq=last_seq + 1)
        finally:
            if not terminal and probe:
                # Client disconnect/cancellation is not a provider failure.
                self._circuit.abandon_probe()

    @staticmethod
    def _unavailable(request: TextRequest, *, seq: int) -> ProviderError:
        return ProviderError(
            event_id=uuid5(request.response_id, "circuit-unavailable"),
            seq=seq,
            at=datetime.now(UTC),
            turn_id=request.turn_id,
            response_id=request.response_id,
            code="provider_unavailable",
            message="Provider is temporarily unavailable.",
            retryable=True,
        )


class _RealtimeSession(Protocol):
    async def configure(self, config: Any) -> None: ...

    async def send_audio(self, chunk: bytes) -> None: ...

    async def commit_audio(self, *, stream_id: UUID, turn_id: UUID) -> None: ...

    async def cancel_response(self, response_id: UUID, reason: str) -> None: ...

    def events(self) -> AsyncIterator[Any]: ...

    async def close(self) -> None: ...


class GuardedRealtimeSession:
    """Admit a realtime provider connection only while its circuit allows it.

    ``configure`` establishes the upstream connection, so only its outcome feeds
    the circuit. Later stream failures are handled by the realtime gateway.
    """

    def __init__(self, session: _RealtimeSession, circuit: ProviderCircuit) -> None:
        self._session = session
        self._circuit = circuit

    async def configure(self, config: Any) -> None:
        probe = self._circuit.state == "half_open"
        if not self._circuit.allow():
            raise ProviderCircuitOpenError("realtime provider circuit is open")
        try:
            await self._session.configure(config)
        except Exception:
            self._circuit.failure()
            raise
        except BaseException:
            if probe:
                self._circuit.abandon_probe()
            raise
        self._circuit.success()

    async def send_audio(self, chunk: bytes) -> None:
        await self._session.send_audio(chunk)

    async def commit_audio(self, *, stream_id: UUID, turn_id: UUID) -> None:
        await self._session.commit_audio(stream_id=stream_id, turn_id=turn_id)

    async def cancel_response(self, response_id: UUID, reason: str) -> None:
        await self._session.cancel_response(response_id, reason)

    def events(self) -> AsyncIterator[Any]:
        return self._session.events()

    async def send_tool_result(self, call_id: UUID, response: Mapping[str, object]) -> None:
        send = getattr(self._session, "send_tool_result", None)
        if send is not None:
            await cast(Callable[[UUID, Mapping[str, object]], Awaitable[None]], send)(
                call_id, response
            )

    async def close(self) -> None:
        await self._session.close()
