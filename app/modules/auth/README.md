# Authentication integration

`install_auth` in `composition.py` mounts the v1 auth/me endpoints (eight with M10 web routes) and
sanitized exception handlers. The application composition owner must call it
once, before startup, with the application's existing database and Redis owners:

```python
from app.modules.auth.composition import install_auth

auth_service = install_auth(
    app, resolved_settings, database=runtime.database, redis=runtime.redis,
)
```

Construct `runtime` before this call; the application lifespan retains ownership
of closing it. Do not mount routes from the lifespan. For conversation/realtime
authentication, `await auth_service.authenticate_access(token)` returns an
`AuthenticatedAccount` with `principal.user_id`, `principal.session_id` and
`user`. The `session_id` is the login's refresh-family UUID. Authentication checks
both the JWT and the current database revocation/deleted-account state. The
existing main application remains the wave integrator's shared-file scope.

The dependency owner should declare `google-auth==2.58.0` directly in
`pyproject.toml` and update `uv.lock`; that exact version is already installed
transitively through google-genai in the frozen baseline. Tests use the existing
cryptography dependency to create transient RSA test keys in memory.

## Server configuration

Settings read process environment only. Configure `GOOGLE_CLIENT_ID` to the Web
OAuth client ID also used by Android's `serverClientId`. Configure
`AUTH_JWT_SECRET` with at least 32 bytes. The development signing default is
rejected in staging/production; those environments also require the client ID.
`GOOGLE_CLIENT_SECRET` is not used for this ID-token flow.

`AUTH_ACCESS_TOKEN_TTL_SECONDS` is constrained to 10–15 minutes.
`AUTH_REFRESH_TOKEN_TTL_DAYS` controls sliding refresh expiry (default 30 days).
`AUTH_NONCE_TTL_SECONDS` defaults to five minutes. Rate controls are
`AUTH_RATE_LIMIT_WINDOW_SECONDS`, `AUTH_NONCE_RATE_LIMIT`,
`AUTH_EXCHANGE_RATE_LIMIT`, `AUTH_REFRESH_RATE_LIMIT`, and
`AUTH_AUTHENTICATED_RATE_LIMIT`.

Nonce/Google exchange/refresh have IP limits. Google exchange additionally limits
supplied device IDs. Valid refreshes have user and device limits; logout/me have
IP and user limits. Rate keys use hashes of identifiers. Redis failure fails
closed. Use a trusted proxy configuration for ASGI client IPs; arbitrary
forwarded headers are not trusted by the router itself.

## Tokens and retry semantics

All mutation endpoints require the contract's `Idempotency-Key` header. This
header never bypasses nonce consumption or refresh reuse detection. Token-bearing
responses are not stored for replay: refresh values persist only as keyed hashes.
A repeated nonce request creates a fresh nonce, even with the same header.
Google exchange consumes a verified nonce once; a repeated exchange returns 401.

Clients must serialize refresh requests per login and atomically replace both
tokens after a successful refresh. Presenting an old/revoked refresh token revokes
the entire login family and returns `invalid_credentials`, including when the
same idempotency key is supplied. After an ambiguous/lost refresh response, the
client must discard that local login and reauthenticate; automatic refresh retries
are unsafe. This policy is tracked for S21/S30 by OI-S12-001.

Access JWTs contain a family ID, so an unexpired access token survives ordinary
rotation but immediately fails after logout/reuse revocation. Logout requires a
valid access token and a refresh token from that same login family; other device
logins remain active. Deleted accounts cannot sign in or authenticate. Account
and device foreign keys cascade on hard deletion; no delete-account endpoint is
introduced in S12.

Family-level PostgreSQL transaction locks serialize rotation, logout and replay
across all generations. Old hashes are retained for reuse detection. S18 should
define expired-family retention/cleanup and deployment secret rotation policy.

## Web cookie sessions and the shared principal (M10)

The web signs in with the same Google Web client ID (`GOOGLE_CLIENT_ID`) and the
same `users` row as Android, keyed by the verified Google `sub` (never email):

1. `POST /v1/auth/web/nonce` returns `{nonce, expires_at}` and sets the HttpOnly
   `mia_web_nonce` cookie; pass the nonce to Google Identity Services.
2. `POST /v1/auth/web/google` with `{"credential": "<ID token>"}` verifies the
   token against the cookie-bound nonce (consumed once) and sets the HttpOnly,
   `SameSite=Strict` `mia_session` cookie (`Secure` outside `APP_ENV=test`,
   30 days, at most 32 live sessions per user). The body is `{user, expires_at}`;
   the session value appears only in `Set-Cookie`, and only its HMAC is stored
   (`web_sessions`).
3. `POST /v1/auth/web/logout` revokes that browser session only.

`RequestAuthenticator` (`principal.py`, available after `install_auth` via
`request_authenticator(app)`) resolves `Authorization: Bearer` first, then the
cookie, to a `RequestPrincipal(user_id: str, method, session_id, user)`.
`GET /v1/me` accepts both. Cookie-authenticated non-GET requests are refused
with 403 `permission_denied` when `Sec-Fetch-Site` is not same-origin or the
`Origin` host differs from `Host` (extra origins: `WebCookiePolicy.trusted_origins`).
The hosted web POC's hub login (`hub:<uuid>` subjects) is not ported: it carries
no Google `sub`.

## Verification and sources

`pytest tests/auth -q` runs deterministic unit/API tests. Set process variables
`MIA_TEST_DATABASE_URL` and `MIA_TEST_REDIS_URL` to dedicated disposable test
instances to include migration-backed repository/concurrency and real Redis TTL
tests. PostgreSQL tests truncate their test database's user data. Never point
them at an application database. `alembic check` verifies model/migration parity.

Signature verification uses Google's library with a bounded certificate request;
explicit claim checks cover audience, issuer, expiry and the supplied nonce.
Identity is keyed only by Google `sub`. Sources:
[Google server verification](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token)
and [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect).
RSA verification tests use the real library with locally generated test keys;
they do not require a real Google account or network credentials.
