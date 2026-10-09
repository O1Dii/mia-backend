"""Provider catalog: which adapters exist, their defaults and capability labels.

Only the primary model path (``chatgpt-plan``) is *verified* (ADR-0014); every other
adapter is labelled ``chat_only`` (no tool guarantees) except the OpenAI-compatible
family, which can carry tools but stays *unverified* until M61 runs the contract suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

ProviderId = Literal[
    "chatgpt-plan",
    "openai",
    "deepinfra",
    "groq",
    "mistral",
    "local",
    "anthropic",
    "gemini",
    "ollama",
]
AdapterKind = Literal[
    "responses_plan", "responses", "chat_completions", "anthropic", "gemini", "ollama"
]
Capability = Literal["tools", "chat_only"]

PLAN_DEFAULT_MODEL: Final = "gpt-6.1-sol"
FALLBACK_MODEL: Final = "zai-org/GLM-5.3-Flash"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: ProviderId
    name: str
    adapter: AdapterKind
    default_url: str
    local: bool
    default_capability: Capability
    can_use_tools: bool
    """False for adapters ported from web_poc as ``chat_only``."""
    verified: bool
    needs_key: bool
    price_per_million: tuple[float, float] | None = None
    """(input, output) USD per 1M tokens when the provider returns no cost itself."""


PROVIDERS: Final[dict[str, ProviderSpec]] = {
    spec.id: spec
    for spec in (
        ProviderSpec(
            "chatgpt-plan",
            "ChatGPT plan (Sign in with ChatGPT)",
            "responses_plan",
            "https://api.openai.com/v1",
            False,
            "tools",
            True,
            True,
            False,
        ),
        ProviderSpec(
            "openai",
            "OpenAI API",
            "responses",
            "https://api.openai.com/v1",
            False,
            "tools",
            True,
            False,
            True,
        ),
        ProviderSpec(
            "deepinfra",
            "DeepInfra",
            "chat_completions",
            "https://api.deepinfra.com/v1/openai",
            False,
            "tools",
            True,
            False,
            True,
            (0.15, 0.50),
        ),
        ProviderSpec(
            "groq",
            "Groq",
            "chat_completions",
            "https://api.groq.com/openai/v1",
            False,
            "tools",
            True,
            False,
            True,
        ),
        ProviderSpec(
            "mistral",
            "Mistral",
            "chat_completions",
            "https://api.mistral.ai/v1",
            False,
            "tools",
            True,
            False,
            True,
        ),
        ProviderSpec(
            "local",
            "LM Studio / local",
            "chat_completions",
            "http://127.0.0.1:1234/v1",
            True,
            "chat_only",
            True,
            False,
            False,
        ),
        ProviderSpec(
            "anthropic",
            "Anthropic",
            "anthropic",
            "https://api.anthropic.com/v1",
            False,
            "chat_only",
            False,
            False,
            True,
        ),
        ProviderSpec(
            "gemini",
            "Google Gemini",
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta",
            False,
            "chat_only",
            False,
            False,
            True,
        ),
        ProviderSpec(
            "ollama",
            "Ollama",
            "ollama",
            "http://127.0.0.1:11434",
            True,
            "chat_only",
            False,
            False,
            False,
        ),
    )
}


class ConnectionValidationError(ValueError):
    """Invalid connection input; the message is safe to show to the user."""


def provider_spec(provider: str) -> ProviderSpec:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ConnectionValidationError("Unknown provider.") from None


def resolve_base_url(spec: ProviderSpec, custom: str | None) -> str:
    """Cloud providers use a fixed address; local ones accept a literal loopback URL only."""
    if not custom:
        return spec.default_url
    if not spec.local:
        raise ConnectionValidationError("A cloud provider uses a fixed API address.")
    parsed = urlsplit(custom)
    # Literal loopback only: no arbitrary destinations, DNS rebinding or redirects (SSRF).
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise ConnectionValidationError(
            "Local address must be http://127.0.0.1:port or http://[::1]:port[/v1]."
        )
    try:
        if not parsed.port or parsed.port < 1024:
            raise ValueError
    except ValueError:
        raise ConnectionValidationError("Use a local port between 1024 and 65535.") from None
    return custom.rstrip("/")


def validate_capability(spec: ProviderSpec, capability: str | None) -> Capability:
    value = capability or spec.default_capability
    if value not in ("tools", "chat_only"):
        raise ConnectionValidationError("Capability must be 'tools' or 'chat_only'.")
    if value == "tools" and not spec.can_use_tools:
        raise ConnectionValidationError("This provider is chat-only in this release.")
    return value  # type: ignore[return-value]
