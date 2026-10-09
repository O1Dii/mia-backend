# ADR-0013: «Войти через Mia» — OAuth 2.1 authorization server for apps

- Status: Accepted
- Date: 2026-10-09
- Sources: [Review](../Mia_Architecture_Review.md) §0, §7.4, §8.1–§8.4, §11 («SDK»)
- Contracts: `contracts/mvp/v1/openapi.yaml` (`/oauth/*`,
  `/.well-known/oauth-authorization-server`, `/v1/chat/completions`, `/mcp`,
  security scheme `miaOAuth`)

## Context

Third-party apps, MindWeaver first, should use the user's model, memory and
tools with the user's consent, without receiving provider keys. MCP clients
(Claude, ChatGPT, Cursor) should be able to connect to Mia itself. First-party
clients already use the public API (dogfooding), so the SDK is mostly a
by-product.

## Decision

1. Mia Core is an **OAuth 2.1 Authorization Server** and resource server.
   Authorization code + **PKCE S256 only**; public clients (SPA, mobile) have
   no secret. Short-lived access tokens, rotating refresh tokens, revocation
   (RFC 7009) and metadata (RFC 8414).
2. Clients are identified by **Client ID Metadata Documents**: `client_id` is
   an HTTPS URL with the client's metadata, as in MCP authorization; no manual
   registration.
3. Scopes:

   | Scope | Grants |
   | --- | --- |
   | `openid profile email` | sign-in identity |
   | `llm.generate` | requests to the user's active model; monthly budget per app |
   | `memory.context` | the core injects relevant memory into the app's model requests |
   | `memory.read:<category>` / `memory.suggest` | raw reads (logged) / suggestions |
   | `tools.use:<plugin_id>` | runs with access to that installed plugin; approvals as usual |
   | `jobs` / `notify` | background jobs and user notifications, with quotas |
   | `mia.install` | register the app as a Mia plugin (MCP server) for the user |

4. APIs for apps: OpenAI-compatible `POST /v1/chat/completions` with
   `model: "mia:default"` and Mia extensions (`mia.context`, `mia.tools`,
   `mia.budget`); `POST /v1/runs` + SSE events; `/v1/jobs`;
   `/v1/memory/context`; `/v1/notify`; and Mia as an MCP server at `/mcp`
   under the same scopes. The OpenAI-compatible endpoint is the only
   mutation exempt from `Idempotency-Key` (`x-mia-idempotency: exempt`).
5. Mia always proxies inference: apps never receive provider keys, so budget,
   context and revocation keep working. Runs from apps use profile `app`.
6. «Подключённые приложения» lists grants, spend and memory access log, and
   revokes further access (already-read data cannot be recalled).

## Consequences

- M50 implements the AS, consent screen, scopes, completions proxy and `/mcp`;
  M51 integrates MindWeaver on both sides; M43 documents the SDK, generated
  from `openapi.yaml`.
- First-party login (M10) and app tokens share one identity; first-party
  clients keep `bearerAuth`.
- Not in the MVP: paid plugins, payouts, issuing provider keys to apps, own
  portable-memory standard (Review §8.4).
