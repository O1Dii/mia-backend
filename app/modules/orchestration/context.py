"""Deterministic context budgeting independent of any provider tokenizer."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from app.providers.ai.base import TextMessage


class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...


class ConservativeTokenEstimator:
    """Provider-neutral upper-biased estimate that handles Cyrillic UTF-8 text."""

    def estimate(self, text: str) -> int:
        return max(1, (len(text.encode("utf-8")) + 2) // 3)


@dataclass(frozen=True, slots=True)
class ContextEntry:
    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True, slots=True)
class UserProfile:
    locale: str
    timezone: str


@dataclass(frozen=True, slots=True)
class BuiltContext:
    system_instruction: str
    messages: tuple[TextMessage, ...]
    estimated_input_tokens: int
    dropped_entries: int


class ContextBudgetExceeded(ValueError):
    pass


class ContextBuilder:
    """Keep stable instructions and newest canonical entries inside a hard budget."""

    def __init__(
        self,
        *,
        max_input_tokens: int = 4096,
        message_overhead_tokens: int = 4,
        estimator: TokenEstimator | None = None,
    ) -> None:
        if max_input_tokens < 1 or message_overhead_tokens < 0:
            raise ValueError("context budgets must be positive")
        self._max_input_tokens = max_input_tokens
        self._message_overhead = message_overhead_tokens
        self._estimator = estimator or ConservativeTokenEstimator()

    def _message_cost(self, text: str) -> int:
        return self._message_overhead + self._estimator.estimate(text)

    def build(
        self,
        *,
        stable_instruction: str,
        entries: Sequence[ContextEntry],
        profile: UserProfile,
        summary: str | None = None,
        tool_results: Sequence[str] = (),
    ) -> BuiltContext:
        instruction_parts = [
            stable_instruction.strip(),
            f"User locale: {profile.locale}. User timezone: {profile.timezone}.",
        ]
        if summary:
            instruction_parts.append(f"Conversation summary:\n{summary.strip()}")
        if tool_results:
            instruction_parts.append(
                "Approved tool results for this turn:\n" + "\n".join(tool_results)
            )
        system_instruction = "\n\n".join(part for part in instruction_parts if part)
        used = self._estimator.estimate(system_instruction)
        if used > self._max_input_tokens:
            raise ContextBudgetExceeded("stable context exceeds the input token budget")

        kept_reversed: list[TextMessage] = []
        for entry in reversed(entries):
            if not entry.text:
                continue
            cost = self._message_cost(entry.text)
            if used + cost > self._max_input_tokens:
                break
            kept_reversed.append(TextMessage(role=entry.role, text=entry.text))
            used += cost
        messages = tuple(reversed(kept_reversed))
        dropped = len(entries) - len(messages)
        if not messages:
            raise ContextBudgetExceeded("no conversation entry fits the input token budget")
        return BuiltContext(
            system_instruction=system_instruction,
            messages=messages,
            estimated_input_tokens=used,
            dropped_entries=dropped,
        )
