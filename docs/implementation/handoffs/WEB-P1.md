# WEB-P1 — web platform localhost POC

> Исторический первый срез. Текущее поведение плагинов/состояния/SDK обновлено в [WEB-P1-R1](WEB-P1-R1.md).

Date: 2026-10-05. Executor: Codex. Request: review current MIA progress, plan the
expanded model-neutral app/plugin platform, implement React web POC with provider
keys, local models and a free plugin store. This is a direct product request,
not execution of an existing Sxx card. No delegated sessions were started.

## Repository state

- Repository: `/Users/aleksey/PycharmProjects/mia-helper`.
- Base: `c5fc60f`, clean `main` at start.
- Result: changes in working tree, no integrated commit yet.
- Android repository and existing application runtime/API were not edited.
- Old W6 remains PARTIAL; S31-R1 remains the outstanding acoustic gate, S33 pending.

## Delivered

`web/`: React 19 + TypeScript + Vite, Motion, Radix dialogs, Lucide, Sonner,
Zod; responsive Russian store, search/category filters, library, install grants,
model settings, chat and author studio. Artwork is repo-native CSS/SVG.
Desktop/mobile previews in `docs/previews/` were visually inspected; no page
JavaScript errors or mobile horizontal overflow were observed.

`app/web_poc/`: standalone FastAPI service on localhost:8001. Native OpenAI
Responses, Anthropic Messages, Gemini generateContent, Ollama Chat; compatible
Chat Completions for DeepInfra, Groq, Mistral and local LM Studio-style servers.
Multiple connections including the same provider, explicit active connection,
test generation, deletion, safe errors, bounded input/output and per-session
admission. No cloud call occurs merely by saving a connection.

Built-in tools: timezone clocks, countdown timer, local-time alarm, persistent
notes/checklist, focus/break sessions, AI summary of user-pasted material. Timer
deadlines live at application level, continue outside their modal, persist on
reload, and expired timers notify once even under React StrictMode.

Custom `community.*` JSON prompt recipes validate against a strict versioned
manifest, require `ai.generate` permission, substitute input, use the active model,
can be installed/updated locally and imported/exported as JSON. Arbitrary code
and external connector permissions are rejected. Shared schema/example are
exported from the runtime schema into `contracts/plugins/v1/`.

Planning: `docs/WEB_PLATFORM_PLAN.md` (prior state, scope, architecture, WEB-P2–P7,
acceptance gates and monetization hypotheses); `docs/PLUGIN_DEVELOPMENT.md`
(actual authoring format, built-in development and future SDK/runtime). README,
launcher and STATUS now link to the new direction without changing old gates.

## Verification

| Check | Result |
| --- | --- |
| `cd web && npm run build` | PASS: strict TypeScript and production bundle; vendor chunks separated |
| `cd web && npm run format:check` | PASS |
| `cd web && npm test` | 12 Chromium journeys PASS |
| `ruff check` / `ruff format --check` on `app/web_poc tests/web_poc` | PASS |
| `mypy app/web_poc tests/web_poc` | PASS, 4 source files |
| `pytest tests/web_poc` | 29 PASS |
| `pytest tests/web_poc tests/test_health.py tests/test_config.py tests/test_composition.py tests/providers tests/realtime/test_contracts.py tests/tools` | 89 PASS / 8 SKIP |
| Existing `contracts/scripts/validate_contracts.py` | PASS: 6 schemas, 18 operations, 36 fixtures, 25 error codes |
| New recipe JSON Schema + exported example | Draft 2020-12 validation PASS |
| npm install/audit | 0 known vulnerabilities reported at installation |
| Visual inspection | Desktop 1440px and mobile 390px; saved previews |
| Live web provider requests | NOT RUN: no actual credentials read or supplied |

Browser journeys cover filters, installation/reload/removal, notes persistence,
clock zones, timer outside modal, pause/break, alarm dispatch, overdue reload
reminders, recipe input substitution, recipe grants/export, local connection,
selected-model chat/summary and absence of keys in browser storage/public replies.

Python tests use only runtime-generated synthetic inputs and MockTransport:
eight native request/response shapes, history preservation, cookie properties,
cross-session access, deletion, error redaction, external-origin/remote-client
blocking, body/response bounds and loopback restrictions. Skipped regressions
require infrastructure or live credentials. This run did not replace full
PostgreSQL/Redis, Android/device or acoustic release gates. Existing Starlette
test-client deprecation warnings and Zod Rollup comment-annotation warnings
are non-failing; project checks themselves pass.

## Data and execution limits

Keys remain server-side in per-session memory, never returned or written to
browser storage/files. Session lifetime is eight hours, process restart clears
keys; expiration cleanup runs each minute. Cookie is HttpOnly/SameSite Strict,
Secure when served through HTTPS. Only localhost peers/hosts and local origins
are accepted; mutating calls require a custom header and JSON. This is local
demonstration authority, not an authenticated public service.

Plugin installs, settings, notes, recipes and deadlines are browser-local.
Chat history lasts until leaving the screen. Browser alarms require an open
active runtime; OS sleep/tab throttling can delay delivery, and reload may
block audio until another user gesture. Each built-in timer/focus/alarm has one
instance. Uninstall cancels its timer and retains its data. Model deletion
does not revoke the provider-issued credential or cancel an already running call.

Cloud keys and model availability must be tested by the owner through «Тест API».
Default names are editable examples. POC local endpoints allow literal loopback
only, not LAN inference/cloud custom base URLs. No automatic paid fallback.
News fetching, Calendar OAuth, smart-home connectors, automatic chat-to-tool
dispatch, billing, public publication and untrusted executable plugin runtime
are subsequent stages, not implemented features.

## Running / next work

See `web/README.md` for two terminal commands: localhost FastAPI on 8001 and Vite
on 5173. No `.env`, database or Redis is required. The development servers were
left running for review at `http://127.0.0.1:5173`; they are ordinary temporary
dev processes and should be relaunched if the session/environment stops them.

Next web stage: WEB-P2 account integration, permanent model connections in a
managed secret vault, server-side plugin installation/grants and shared existing
orchestration. Then RSS and calendar read-only integrations, durable jobs and
public recipe publication. Product questions are recorded in WEB-P1 inbox;
they do not block the delivered local POC.
