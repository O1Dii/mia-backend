# WEB-P1-R3 — allow the Google return to open MIA

Date: 2026-10-06. Status: COMPLETED — fix verified and deployed.
Direct follow-up to WEB-P1-R2; no changes to old Android/voice gates. Working tree
on baseline `c5fc60f`; no new commit created.

## Problem and fix

The owner completed Google authorization and the browser returned to
`/mia/?signin=google`, but MIA responded with 403 “Запрос с внешнего сайта
отклонён.” The middleware rejected every `Sec-Fetch-Site: cross-site` request,
including the harmless top-level GET for the public React entry document.
The previous live verification stopped at the Google redirect and did not cover
the return navigation. This is an application boundary bug, not a missing Google
callback registration. The configured shared callback remains
`https://mindmapgen.xyz/api/auth/google/callback`.

Hosted middleware now permits only GET/HEAD to the static entry `/`, with
`Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`. Nginx strips `/mia/`
before that request reaches the app. Origin and cross-site checks still apply
to all API requests, mutation requests, iframe loads and non-navigation fetches.
Loading the page does not create an account session; the usual same-origin
hub exchange still requires the trusted Google session.

## Verification

- New backend regression FAILED with 403 before the fix; now PASS. Covers the
  landing page, HEAD, absence of automatic sign-in, cross-site API/asset access,
  iframe/fetch rejection, mutation rejection and missing hub-cookie denial.
- `uv run --frozen pytest tests/web_poc tests/auth/test_google_verifier.py -q`:
  **71 passed** (58 web + 13 Google verifier). Ruff and mypy PASS.
- amd64 Docker production build/TypeScript/Vite PASS; Prettier PASS.
- New `web/scripts/verify-hosted-auth-return.mjs` uses a temporary external-origin
  HTTP page and actual 303 browser redirect to the live MIA URL. It asserts real
  Chromium Fetch Metadata headers, landing status/UI, anonymous workspace 401
  and cross-site API 403. It FAILED with 403 on the old public release.
- After deployment, the same actual Chromium 303 return regression PASS:
  cross-site document navigation 200, login UI rendered without JS errors,
  anonymous workspace 401 and cross-site API 403. The script asserts browser-
  generated Fetch Metadata headers; it does not inject them into navigation.
- Public HTTPS smoke PASS: return URL HTML 200, API boundary 403, anonymous
  workspace 401, all five hub cards and existing app routes preserved.
- Server MIA healthy; observed image
  `sha256:4df36b9d08193c05f4468e66d3557d5d4f71e8ca5f778f2ffcf20ada72505a06`.
  Neighboring container uptimes unchanged; no changes to shared Google config.
- Shell/Prettier/Ruff-format/diff checks PASS. The 20 prior UI journeys remain
  previous verification; this backend-only fix added the independent live return
  regression rather than rerunning unchanged frontend behavior.
- No Google account, actual OAuth grant, provider key or paid model call is used
  by this regression. Google consent remains the owner's interactive login.

Release completed using the already authorized environment-based VPN/SSH connection and
isolated release wrapper; no credential files or values are displayed. Existing
accounts/plugin data stay in the mounted SQLite directory. Runtime keys/grants
expire on restart, as documented in WEB-P1-R2.

## Technical references

[Fetch Metadata: redirects](https://www.w3.org/TR/fetch-metadata/#redirects) describes
cross-site classification along redirect chains and distinguishes document
navigation from other requests. [Google web-server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)
documents the existing registered redirect URI flow; MIA adds no new callback.
