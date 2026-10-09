"""Run profile budgets (ADR-0011 §1). Values come from ``Settings`` (``RUN_*``)."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.ports.types import RunProfile

SPEECH_HARD_LIMIT = 300
"""``assistant-response.schema.json`` ``speech.maxLength``."""


@dataclass(frozen=True, slots=True)
class ProfileLimits:
    max_steps: int
    """Model calls per run; the last allowed step is offered no tools."""
    max_tool_calls: int
    timeout_s: float
    """Wall-clock budget excluding time spent waiting for a human approval."""
    speech_max_chars: int = SPEECH_HARD_LIMIT
    ack_delay_ms: int | None = None
    """Emit ``speech.ack`` when ``response.final`` is not ready after this delay."""
    stream_deltas: bool = False
    """Emit ``message.delta`` drafts (plain-text model mode only)."""

    def __post_init__(self) -> None:
        if self.max_steps < 1 or self.max_tool_calls < 0 or self.timeout_s <= 0:
            raise ValueError("run limits must be positive")
        if not 1 <= self.speech_max_chars <= SPEECH_HARD_LIMIT:
            raise ValueError("speech limit must be within the contract hard limit")


@dataclass(frozen=True, slots=True)
class RunLimits:
    voice: ProfileLimits
    chat: ProfileLimits
    job: ProfileLimits
    app: ProfileLimits

    def for_profile(self, profile: RunProfile) -> ProfileLimits:
        limits: ProfileLimits = getattr(self, profile)
        return limits

    @classmethod
    def from_settings(cls, settings: Settings) -> RunLimits:
        ack = settings.run_voice_ack_delay_ms
        return cls(
            voice=ProfileLimits(
                max_steps=settings.run_voice_max_steps,
                max_tool_calls=settings.run_voice_max_tool_calls,
                timeout_s=settings.run_voice_timeout_seconds,
                speech_max_chars=settings.run_voice_speech_max_chars,
                ack_delay_ms=ack if ack > 0 else None,
            ),
            chat=ProfileLimits(
                max_steps=settings.run_chat_max_steps,
                max_tool_calls=settings.run_chat_max_tool_calls,
                timeout_s=settings.run_chat_timeout_seconds,
                stream_deltas=True,
            ),
            job=ProfileLimits(
                max_steps=settings.run_job_max_steps,
                max_tool_calls=settings.run_job_max_tool_calls,
                timeout_s=settings.run_job_timeout_seconds,
            ),
            app=ProfileLimits(
                max_steps=settings.run_app_max_steps,
                max_tool_calls=settings.run_app_max_tool_calls,
                timeout_s=settings.run_app_timeout_seconds,
                stream_deltas=True,
            ),
        )
