# WEB-P1-R1 — executable plugin runtime, dashboard and SDK

Date: 2026-10-05. Status: COMPLETED for local POC scope; not a public release.
Direct user follow-up, outside the Sxx wave dispatcher. Working tree on baseline
`c5fc60f`; no new commit created. Previous WEB-P1 files preserved and extended.

## Problem and result

The first POC's timer was browser-only and chat had no tools. News required pasted
text. The user asked for plugin = application/interface + skill, controlled by
whichever model they selected, with downloadable apps and a configurable dashboard.

Now each installed plugin has server-side skills, typed actions and shared state.
Chat injects installed tools, calls the chosen model, executes validated actions,
returns results to the model, and the web/SDK views observe those changes.
Timer start/pause/resume/cancel, notes, clocks and alarms share this runtime.

- Native loops: OpenAI Responses, Anthropic, Gemini, DeepInfra, Groq, Mistral,
  Ollama and local OpenAI-compatible. Preserve continuation data (including GLM
  reasoning_content and Gemini thoughtSignature); optional explicit JSON mode.
- Per-session authoritative active connection, install registry, states/layout,
  bounded audit, request idempotency, limits, dependency checks and scoped access.
- Internet connector: public HTTPS with validated/pinned DNS, bounded reads and
  redirect checks; Bing search RSS, Google News RSS filtered to recent 48 hours.
- Digest uses retrieved titles/snippets, selected model and sources; optional
  explicit 30/60/180-minute refresh while local API/session remains alive.
- Dashboard: three tile sizes, reorder with drag/drop or accessible controls,
  remove/add view independently of uninstall; state synchronized by polling.
- Manifest v2: typed actions, state.patch/model.prompt, fields/HTML view, downloads.
  Legacy recipe import converts to v2. HTML runs in opaque iframe with scoped bridge.
- `packages/mia-sdk/`: model-neutral JS client, revocable device-style pairing,
  independently downloadable HTML timer. UI approves grants; tokens access only
  their installation and the currently selected model, never provider keys.
- Updated plans, author documentation, examples/schema, desktop/mobile previews.

## Verification

- `uv run --frozen pytest tests/web_poc -q`: **50 passed**.
- Ruff lint/format and strict mypy for `app/web_poc tests/web_poc`: PASS.
- `npm run build`: TypeScript + production Vite build PASS.
- `npm run format:check`: PASS. `node --check packages/mia-sdk/mia-sdk.mjs`: PASS.
- `npm test`: **13 Chromium journeys passed**. Critical journeys run through real
  FastAPI and a temporary local HTTP model fixture, not intercepted browser replies:
  chat → model tools → runtime → dashboard; layout persistence; independent app
  pairing → model → shared timer → revocation; recipe server prompt; sandbox HTML bridge.
- All eight native protocol loops tested with MockTransport, including preserved
  reasoning/signatures, native idempotency and partially failed continuation.
- JSON compatibility, malformed commands, unknown/uninstalled actions, expired/revoked
  SDK access, cross-user and cross-plugin isolation, credential boundaries, manifest
  upgrades, RSS freshness and private IP restrictions tested without live credentials.
- Manifest examples `focus-board.json` and `focus-html.json`: Draft 2020-12 JSON
  Schema validation + Pydantic validation PASS.
- Real credential-free source checks: Google News RSS returned 8 recent items for
  Russian AI topic; Bing RSS returned 8 for OpenAI. A Russian general search returned
  no results; empty source responses remain valid and must not become invented news.
- Desktop/mobile screenshots inspected; 390px viewport has no horizontal overflow.
- Existing WEB-P1 regression results are historical; old Android/backend source and
  voice gates are unchanged. Real paid GLM/provider calls were **not** performed.

## Limits / next work

1. All runtime state/connections/grants live in memory for max 8 hours, cleared by
   API restart. Reload preserves them. API was restarted to load the implementation;
   owner must re-add their connection/key through the UI. Secret-value files untouched.
2. POC subject_id is local session identity, not existing users/auth integration.
   WEB-P2 must introduce persistent user/install identity, database, vault and PKCE/device auth.
3. Old local installed IDs/recipes migrate; previous browser note/timer values are
   retained in storage but not automatically copied into runtime. Persistent migration
   belongs to WEB-P2; no claim of durable data or cross-device sync.
4. Local HTML sandbox is a POC boundary. Publication requires separate origin,
   immutable/pinned versions, verified publisher/client identity, signed bundles,
   navigation/resource policy and stronger abuse controls. Downloads may be author links.
5. Custom author actions currently patch state or run prompts; arbitrary server handlers,
   full connector SDK/workflow engine, Android/iOS packaging and public store upload are future work.
6. Digest summarizes RSS metadata, not full articles. Scheduler is ephemeral; timer sound
   requires an open browser and unlocked audio. No production notification guarantee.
7. Idempotency caches are bounded/in-memory; new request IDs can intentionally repeat
   actions. Revocation blocks new requests, not an already executing provider turn.
8. No billing/payment added. Native tool support depends on selected model; JSON mode
   still requires instruction following. No silent provider substitution.

## Review

Open `http://127.0.0.1:5173`, install Timer, select the actual model (DeepInfra example
`zai-org/GLM-5.3-Flash`), ask “Поставь таймер на 5 минут”, then open Dashboard.
“Приложения и доступ” contains SDK app download and pairing. “Для авторов” accepts
v2 examples. Details: `docs/PLUGIN_DEVELOPMENT.md`, `docs/WEB_PLATFORM_PLAN.md`,
`packages/mia-sdk/README.md`, `web/README.md`.
