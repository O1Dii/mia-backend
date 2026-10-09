# ADR-0014: One primary product model until the MVP is accepted

- Status: Accepted (MW0 integration, 2026-10-09; outcome of M01, `spikes/m01/DECISION.md`)
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

## Decision (outcome of M01)

Transplanted verbatim by the MW0 integrator from the M01 handoff
([M01](../implementation/handoffs/M01.md), [DECISION](../../spikes/m01/DECISION.md)).

**Decision.** Основная модель MIA на время разработки и в MVP — GPT на плане ChatGPT через
официальный Sign in with ChatGPT (OAuth Authorization Code + PKCE, loopback
`http://127.0.0.1:<port>/auth/callback`, динамическая регистрация клиента
`dynamic_agent_client` → выданный `oaiapp_…`, scope
`openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`,
`resource=https://api.openai.com/v1`) и Responses API (`POST https://api.openai.com/v1/responses`,
`store:false`, `stream:true`, tools только function). Адаптер `chatgpt-plan` — отдельный
провайдер Model Gateway (M11). Резервный и generic OpenAI-compatible провайдер —
DeepInfra `zai-org/GLM-5.3-Flash` (chat completions, `reasoning_content` возвращается в
следующих ходах). Токены Codex CLI и ChatGPT в обход официального потока не используются.

**Evidence.** Spike M01 (2026-10-09, `spikes/m01/DECISION.md`): вход и refresh с ротацией
прошли; function calling с продолжением прошло на `gpt-6.1-sol`; видимых моделей 7;
first-event p50 1340 мс, первый content-токен p50 2411 мс; GLM: 1405/2763 мс.

**Consequences.** Нужны: сериализованный refresh (access ≈ 1 ч, refresh ≈ 30 дней с
ротацией), проверка подписи ID-токена по JWKS, шифрованное хранение токенов, обработка
`subscription_sharing_usage_limit_exceeded` как паузы с переключением на запасного
провайдера. Использование плана допустимо для локально/самостоятельно размещённого
приложения владельца; многопользовательское облако требует заявки OpenAI (OI-M01-002).
Численные лимиты Plus неизвестны (OI-M01-001).

## Consequences

- M11 implements the gateway and the adapter of the chosen model first; M12
  runs its contract tests against that model (and against the fake gateway in
  CI).
- The response splitter for models without structured output (ADR-0011) is
  verified on the chosen model.
- If the primary model is GPT on Plus, multi-user use needs OpenAI's interest
  form; the pilot stays single-operator until then (Review §5.5).
