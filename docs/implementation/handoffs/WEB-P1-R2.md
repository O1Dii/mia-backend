# WEB-P1-R2 — plugin pages, Google accounts, browser voice and release

Date: 2026-10-05; deployment completed 2026-10-06. Status: COMPLETED for
shared-server POC implementation/publication scope. Real Google consent was not
automated for the owner; they complete it through the published login button.
Direct user follow-up outside the Sxx dispatcher. Working tree on `c5fc60f`;
no new commit created. Existing Android/backend voice gates are unchanged.

## Result

- Store links open deep-linked plugin pages with expanded descriptions, example
  commands, permissions, downloads, two actual screenshots and real reviews.
  Installed-plugin users can create/update/delete their own review; there are no
  fabricated ratings. Fourteen screenshots cover the seven built-in plugins.
- Hosted Google-only authentication gates personal workspace access. Existing
  shared hub Google OAuth is reused, with server-side verification of its session
  and rejection of local/demo identities. Separate Google Identity Services flow
  with signature/audience/nonce verification is also implemented.
- Persistent SQLite accounts, revocable hashed 30-day sessions, installations,
  plugin state/layout and reviews. Cross-account isolation and restart persistence
  tested. Provider keys/connections and SDK grants remain ephemeral and are not
  included in persisted workspace. Local development stays anonymous by default.
- Chat has browser dictation, editable transcript/manual send, answer speech,
  opt-in automatic speech and stop controls. Unsupported/denied microphones keep
  keyboard input available. No additional provider or voice service key required.
- Vite/API/SDK support `/mia/`; downloaded timer points at the hosted platform.
  Inline view bridge now includes only its browser-compatible scoped implementation.
  Hosted loopback models are disabled pending a local companion connector.
- Reviewed amd64 Docker image, hardened Compose, dedicated nginx location and
  MIA hub card. Isolated release checks port 8004, starts only MIA, requires auth,
  merges existing cards and rolls back touched configs/image on release failure.
  Connection wrapper uses only the existing shared script's `--ssh` mode.

## Verification

- `uv run --frozen pytest tests/web_poc tests/auth/test_google_verifier.py -q`:
  **70 passed** (57 web + 13 existing Google verifier).
- Ruff lint/format and mypy for `app/web_poc tests/web_poc`: PASS.
- TypeScript/production Vite build and Prettier: PASS.
- Chromium journeys: **20 passed**. Seven new journeys cover plugin pages,
  hosted login gate, review UI, voice output/input/errors, single hub exchange
  under React StrictMode, expiry lock and unsupported recognition fallback.
- API tests cover nonce replay/logout, trusted hub identity, local-user rejection,
  durable isolated state/reviews after restart with no keys, hosted fail-closed
  configuration, secure cookie path and hosted loopback denial.
- amd64 Docker image built, loaded and run locally as UID 10001/read-only with
  temporary SQLite. Container healthy; auth required/hub enabled, anonymous
  workspace 401, assets/screenshots 200 and actual built Google login verified
  through `/mia/` proxy. No JS errors or mobile overflow at 390px. Smoke proxy
  mapped only its test origin to production origin; server checks retained.
- Shell syntax, Compose config and `git diff --check`: PASS.
- Preview `docs/previews/mia-google-login.png` is the actual built login page;
  no real Google account was used. Plugin desktop/mobile previews also saved.
- Public HTTPS smoke PASS: `/mia/`, build assets and timer screenshots 200;
  Google auth required, anonymous workspace 401; all five hub cards present;
  existing `/`, `/app/`, `/source-refiner/`, `/knowly/`, `/knowly/parents/` 200.
- Live Chromium PASS: actual hub MIA tile opens `/mia/`; Google button visible;
  OAuth redirect to `accounts.google.com` with existing HTTPS callback configured;
  no JS/resource errors and no horizontal overflow at 390px. Live screenshots saved.
- Server PASS: MIA healthy, UID 10001/read-only/384MB; data directory owner 10001
  and mode 700; nginx valid; neighboring container uptimes unchanged.
- No paid model calls or Google consent in the owner's account performed.

## Published release

URL: **https://mindmapgen.xyz/mia/**. The main hub has the MIA card, order 40.
Existing four cards preserved. Non-secret bundle `/tmp/mia-release` approximately
108 MiB; uploaded SHA256 verified before loading. Server observed Docker image:
`sha256:7a7a92dc068617758192f3597b23f12720710f4ea9037345227d9cd4857e4736`.
Instructions: [deploy/README](../../../deploy/README.md).

The owner explicitly authorized using stored deployment env values only as
variables, without inspecting/displaying secret files. The wrapper loaded the
existing environment with tracing disabled and suppressed diagnostics and passed
`SERVER_PASSWORD` to the existing shared VPN/SSH tool through its environment,
avoiding its password-file grep fallback. Secret values were never displayed,
logged, committed, embedded in command arguments or copied into MIA. The shared
operational tool managed VPN authentication without agent inspection of credentials.

The isolated script started MIA, added its nginx include and merged its hub card;
neighboring services were not restarted. A public auth JSON check confirmed the
release after nginx reload. The initial proxy request briefly reached an old nginx
worker and returned the hub's HTML with HTTP 200; the release script now retries
and validates required-auth JSON to prevent treating that response as success.
Local shell/Compose/diff checks passed after that script-only change.

Live screenshots: `mia-live-hub.png`, `mia-google-login.png`,
`mia-google-login-mobile.png` in `docs/previews/`. Google consent remains the
user's interactive login, rather than an automated test or release dependency.

## Limits

API keys/connections expire at 8h/restart and must be entered again. SDK grants
remain scoped/in-memory/one hour and require re-pairing after restart. Logout
does not cancel a running model request or separately approved SDK grant.
Timer/alarms need an open browser for sound; digest scheduler is ephemeral.
Browser recognition support varies and may use the browser's external service.
No local-model companion, vault, community package publication or payments yet.

## Follow-up

The owner's real Google login revealed a blocked cross-site return navigation
which this initial release's outbound-only OAuth smoke did not cover.
[WEB-P1-R3](WEB-P1-R3.md) fixes and verifies that return on the published server.
