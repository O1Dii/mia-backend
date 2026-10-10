# MIA MVP contracts v1

Machine-readable boundary of the MVP (architecture variant E,
`docs/Mia_Architecture_Review.md`; ADR-0010…0015). Owner: M00. Later sessions
may only make **additive** changes that their card allows, and record them in
their handoff. Old `contracts/{openapi,realtime,ai,errors,plugins}/v1|v2` stay
unchanged and keep serving the PoC until their consumers move.

| File | What it defines | Implemented by |
| --- | --- | --- |
| `common.schema.json` | ids, timestamps, run profiles, risk, `args_hash`, `ui://` URIs, public error, `screenAction` (M41-R1) | — |
| `run-events.schema.json` | SSE events of `GET /v1/runs/{id}/events`: `sequence`, `cursor`, 14 event types (`speech.ack` added by M12) | M12 (server), M22/M30 (clients) |
| `assistant-response.schema.json` | final answer `{speech, display, followups}` | M12 |
| `error-codes.json` | public error codes introduced by MVP sessions (`code`, `http_status` or null for event-only codes, `retryable`, `public_message`, `owner`); checked by the validator; codes shared with `errors/v1` keep its `retryable` (OI-M00-001) | M12, then additively M10/M11/M13/M15/M16/M17/M50 |
| `plugin-manifest.schema.json` | `mia.json`: `builtin` / `remote` / `declarative` server, tools risk overlay, tiles, permissions | M14, M15 |
| `approval.schema.json` | `Approval`, `ApprovalDecision`, `ApprovalDecisionRequest` bound to `args_hash` | M13 |
| `device-tools.schema.json` | device tool registration, call, result, errors | M13 (core), M23 (Android) |
| `device-access.schema.json` | ADR-0016: `DeviceConsent` (`Approval.device_consent`, only the target device decides), grants, access log, capability settings, queued calls, FCM data `device_consent` / `device_tool_call`, manifest `requires.deviceCapabilities` items | M70 (server), M71 (Android), M72 (store) |
| `memory.schema.json` | `Fact`, `Person`, `Preference`, `MemorySuggestion` | M17 |
| `notifications.schema.json` | M77: `NotificationSettings` (channels with connected / not connected, per-channel switch, «event type × channel» table), `UpdateNotificationSettingsRequest`, the Telegram bot's question; explicit `delivery.channels` win over it | M77 (server, web), M71 (Android) |
| `job.schema.json` | `Job`, `Schedule`, `InboxItem`, create requests | M16 |
| `openapi.yaml` | path skeleton; bodies `$ref` the schemas above | M10…M50 |

Conventions:

- JSON Schema Draft 2020-12; objects reject unknown fields. `$id` is
  `https://contracts.mia.local/mvp/v1/<file>`; cross-file `$ref`s are relative.
  Tools resolve them locally (never over the network).
- API payloads use `snake_case`; `mia.json` uses `camelCase` like the Review
  example and MCP.
- `args_hash` = `sha256:` + hex SHA-256 of canonical JSON (sorted keys,
  `(",", ":")` separators, UTF-8, non-ASCII kept). Reference implementation:
  `app.core.ports.approval_service.canonical_args_hash`.
- Run events: `sequence` starts at 1 and grows by 1 per run; `cursor` is
  opaque and doubles as the SSE `id`; `run.completed`, `run.failed`,
  `run.cancelled` are terminal. `fixtures/run-events.json` is a catalog with one
  event per type, not a realistic stream.
- `examples/<schema>/*.json` validate against the schema root,
  `examples/<schema>/<Def>/*.json` against `#/$defs/<Def>`.
- Every non-GET `/v1/*` operation takes `Idempotency-Key`, except operations
  marked `x-mia-idempotency: exempt` (OpenAI-compatible endpoints). Every
  inline response declares `X-Request-ID`.

Validate from the repository root:

~~~bash
.venv/bin/python contracts/scripts/validate_contracts.py
~~~

Python ports that consume these contracts live in `app/core/ports/`; fakes in
`tests/fakes/`; contract tests in `tests/contracts/mvp/`.
