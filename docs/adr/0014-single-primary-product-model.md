# ADR-0014: One primary product model until the MVP is accepted

- Status: Proposed — decision pending M01 (`spikes/m01/DECISION.md`)
- Date: 2026-10-09
- Sources: [Review](../Mia_Architecture_Review.md) §5.5, §9, §2;
  [MVP plan](../implementation/MVP_SESSION_PLAN.md) §3 («Модель продукта»), M01, M61
- Contracts: `app/core/ports/model_gateway.py` (`DEFAULT_MODEL = "mia:default"`,
  `ModelInfo.verified`)

## Context

The PoC talks to eight providers through `app/web_poc/providers.py`, but tool
calling, structured output and voice latency differ per model. Debugging the
run engine, the router and the voice cascade on several models at once
multiplies failures. Two candidates exist for the single primary model:
GPT on a ChatGPT Plus plan through the official «Sign in with ChatGPT» flow,
or DeepInfra GLM-5.3-flash through an OpenAI-compatible API.

## Decision (fixed part)

1. All contract scenarios, debugging and voice are brought to work on **one
   primary model** first. Product code addresses it as `mia:default`; the
   Model Gateway resolves it per user.
2. A second model is added only in M61, after M60 acceptance, by running the
   same contract test suite; a model that passes is marked `verified`.
   Other adapters stay «chat-compatible, actions without guarantees».
3. Subscriptions are used only through official OAuth flows (Sign in with
   ChatGPT, OpenRouter OAuth PKCE). Reusing Codex CLI or ChatGPT session
   tokens is forbidden. Claude and Gemini subscriptions are not connected.
   The `chatgpt-plan` adapter is a separate provider next to the OpenAI API.

## Decision (pending M01)

<!-- MW0 integrator: replace this section with the outcome of
     spikes/m01/DECISION.md and set Status to Accepted. -->

- Primary model: **TBD by M01** — GPT on ChatGPT Plus if all four spike
  checks pass (client registration and sign-in, Responses API with
  `store: false` + `stream: true`, function calling on the plan, models and
  limits of Plus); otherwise DeepInfra GLM-5.3-flash (default fallback).
- Evidence: `spikes/m01/DECISION.md`.
- Adapter and configuration names for M11: TBD by M01.

## Consequences

- M11 implements the gateway and the adapter of the chosen model first; M12
  runs its contract tests against that model (and against the fake gateway in
  CI).
- The response splitter for models without structured output (ADR-0011) is
  verified on the chosen model.
- If the primary model is GPT on Plus, multi-user use needs OpenAI's interest
  form; the pilot stays single-operator until then (Review §5.5).
