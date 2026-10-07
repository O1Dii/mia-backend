# MIA on the shared server

Published: **https://mindmapgen.xyz/mia/**. Hub card `mia`, order 40, is visible
at https://mindmapgen.xyz/. Public release verified on 2026-10-06. The private
server uses the existing VPN/SSH connection mechanism.

## Reviewed release

`Dockerfile.web` builds React with `/mia/` as its base and serves it from the
separate web FastAPI process. The Docker context uses an allowlist and excludes
secret-value files. The image runs as UID 10001 with a read-only filesystem,
384 MB memory limit, loopback-only host port 8004 and `/opt/mia/data` for SQLite.
Neither PostgreSQL nor Redis is required for this POC.

```sh
bash deploy/build-web.sh
```

The non-secret bundle is `/tmp/mia-release`: amd64 image, Compose, nginx location,
hub card and server release script. `release-mia.sh` checks port availability,
starts only MIA, verifies that Google login is required and anonymous workspace
access returns 401, installs `/etc/nginx/mindmapgen.d/mia.conf`, and merges the new
card with existing cards. It backs up the touched configuration and previous MIA
image and rolls them back on failure. It reloads nginx without restarting other
applications. Uploads verify the image checksum before release.

## Environment-based connection

The owner authorized using stored deployment values **only as environment
variables**, without inspecting or displaying the secret files. The wrapper
uses `source` with tracing disabled and diagnostics suppressed to load the
existing deployment environment, then exports `SERVER_PASSWORD` for the shared
VPN/SSH authentication tool. It never prints secret values, copies them into MIA
or puts them in command arguments. The shared script receives the password from
its environment, so its password-file grep fallback is not used. VPN is managed
by that existing operational tool; its credential configuration is not inspected.

```sh
bash deploy/publish-via-shared-server.sh
```

Alternatively, export `SERVER_PASSWORD` through an approved loader first.
`MIA_AUTH_ENV` can select the existing deployment environment path;
`MIA_SHARED_SCRIPT` selects the shared server's authentication script. Values
must never appear in logs or committed files.

The wrapper uses the shared script's `--ssh` mode, its authenticated connection
and resumable uploader. It never invokes the neighboring project's deploy mode.
No password, OAuth secret or VPN private key is copied into MIA. After publication,
check `/services.json`, `/mia/`, `/mia/api/web/auth/session`, anonymous
`/mia/api/web/workspace` (401), and existing applications. Real Google consent
is a browser step; automated tests cannot sign in to the owner's account.

## Google login

Production sets `MIA_PUBLIC_ORIGIN=https://mindmapgen.xyz`, `MIA_BASE_PATH=/mia`,
and `MIA_GOOGLE_HUB=1`. The login button uses the server's existing Google OAuth
endpoint with `return_to=/mia/?signin=google`. On return, MIA verifies the root
HttpOnly session by calling the fixed same-origin `/api/auth/me` server-to-server.
Local/demo users are rejected. It creates its own revocable 30-day Secure,
HttpOnly, SameSite cookie under `/mia/` and a persistent account identity.
No new Google client secret or callback registration is needed for this flow.
Logout closes the MIA session; the shared hub session remains available to other
apps. Shared OAuth availability is a dependency of new MIA logins.

Alternatively, a separate deployment can set public `MIA_GOOGLE_CLIENT_ID` and
use Google Identity Services with nonce-bound, server-verified ID tokens.
Hosted startup refuses to run without configured Google authentication and a
persistent database. Default localhost development stays anonymous, so it does
not show the Google gate. Preview: [login screen](../docs/previews/mia-google-login.png).

## Persistence and limits

SQLite persists accounts, session hashes, installations, plugin state/layout and
reviews. Provider keys, model connections, SDK grants and in-flight operations
remain in memory, expiring after eight hours or restart; keys must be re-entered.
SQLite backups should be handled as private account data. Browser history is
ephemeral. Hosted loopback model endpoints are disabled: a cloud server cannot
reach the user's Ollama or LM Studio. A local companion connector is future work.
Timer audio needs an open browser. Dictation uses browser SpeechRecognition
where supported; speech synthesis reads answers. The browser may process audio
through its own service; neither function uses the selected provider's API key.
This is a shared-server POC, not the complete WEB-P2 vault/device rollout.

## Verification evidence

- Web API + existing Google token verifier: 71 tests passed (including the Google return regression).
- Chromium journeys: 20 passed; TypeScript/Vite, Prettier, Ruff and mypy passed.
- amd64 release container: healthy; required Google login, anonymous workspace
  401, real built login page/assets and plugin screenshots under `/mia/`, and
  390px layout without overflow verified. Local smoke proxy mapped the test
  origin to the configured HTTPS origin; production-origin checks were retained.
- Shell syntax and Compose configuration passed. Public HTTPS checks passed:
  all five hub cards preserved; MIA assets/screenshots 200; auth required;
  anonymous workspace 401; existing applications 200. Live Chromium followed
  the hub card to the real Google login page, verified mobile layout and the
  OAuth redirect to Google with the existing callback URL. No JS/resource errors.
- Server container healthy, UID 10001/read-only/384 MB; data directory mode 700;
  nginx configuration valid; neighboring container uptimes unchanged.
- Google account consent was not automated or performed for the owner; it is
  the normal interactive step when they press the published login button.
- Server Docker image ID:
  `sha256:4df36b9d08193c05f4468e66d3557d5d4f71e8ca5f778f2ffcf20ada72505a06`.
- [Live hub](../docs/previews/mia-live-hub.png),
  [live login](../docs/previews/mia-google-login.png),
  [mobile login](../docs/previews/mia-google-login-mobile.png).
- The nginx reload check validates auth JSON, because an old worker can briefly
  return the hub's HTML with HTTP 200. Future releases retry that condition.

## Google return regression

The return URL is `/mia/?signin=google`; the registered Google callback remains
`https://mindmapgen.xyz/api/auth/google/callback`. No MIA-specific callback is
needed. Hosted middleware allows the top-level GET/HEAD entry document after a
cross-site OAuth redirect, while cross-site API/mutations remain blocked.
Verify the actual redirect navigation against a running public release:

```sh
node web/scripts/verify-hosted-auth-return.mjs
```

The script is anonymous and uses a temporary loopback page as its external
origin, then a real 303 browser redirect. It never signs into a real Google
account or accesses a provider key. Handoff: [WEB-P1-R3](../docs/implementation/handoffs/WEB-P1-R3.md).

Verified after the WEB-P1-R3 deployment: actual Chromium cross-site 303 landing
200/UI rendered; anonymous workspace 401, cross-site API 403; MIA healthy and
existing services preserved. The same browser regression returned 403 before
this fix. No Google account settings were changed.
