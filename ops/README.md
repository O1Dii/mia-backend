# Internal beta operations

Use one backend container behind a TLS reverse proxy with managed PostgreSQL and
Redis. Keep `DATABASE_URL`, `REDIS_URL`, `AUTH_JWT_SECRET`, `GOOGLE_CLIENT_ID` and
`GEMINI_API_KEY` in the deployment secret store; never put their values in files
committed to this repository or in command output. Configure the proxy for
HTTPS/WSS, terminate TLS there and set `FORWARDED_ALLOW_IPS` to the proxy address,
otherwise every per-IP auth limit shares one bucket. Run one backend
worker/instance: realtime session claims, the active-session limit and the
provider circuit breakers are process-local.

## Release

Build and publish the immutable image from `Dockerfile`; set `MIA_IMAGE` to its
digest in the deployment environment. The image runs as an unprivileged user and
needs no writable filesystem. Before switching traffic, take a backup and run the
migration release step with the same image and target database:

```sh
docker compose -f ops/staging.compose.yml --profile release run --rm migrate
docker compose -f ops/staging.compose.yml up -d backend
curl --fail --silent http://127.0.0.1:8000/health/ready
```

The release job must finish successfully before the backend starts: `/health/ready`
reports 503 until PostgreSQL is at the Alembic head that is packaged in the running
image, so a forgotten migration or a rolled-back image never receives traffic.
`/health/live` checks only process liveness. Keep the loopback binding in
`staging.compose.yml` behind the TLS proxy.

## Admission controls and kill switches

`VOICE_ENABLED` and `TEXT_ENABLED` are emergency kill switches; restart the backend
after changing them. Daily per-user counters (`MAX_DAILY_TEXT_TURNS`,
`MAX_DAILY_LIVE_SESSIONS`) use Redis UTC days and fail closed when Redis is
unavailable. Admission also enforces `MAX_ACTIVE_LIVE_SESSIONS_PER_USER` and a
`MAX_LIVE_SESSION_SECONDS` WebSocket cap, and opens a provider circuit after
`PROVIDER_FAILURE_THRESHOLD` consecutive provider failures for
`PROVIDER_COOLDOWN_SECONDS`, with a single recovery probe. These are conservative
admission controls, not a provider billing ledger: validate actual token and audio
spend in the provider console before widening the beta.

## Backup and restore drill

Schedule a daily encrypted PostgreSQL snapshot with a retention policy in the
managed database service. For a portable encrypted logical backup, run `pg_dump`
from a restricted release runner into an encrypted destination; never leave an
unencrypted dump on shared storage or in this repository. Record snapshot ID and
time in the operator runbook.

Monthly, run the drill against a disposable restore database:

```sh
MIA_SOURCE_URL=... MIA_RESTORE_URL=... sh ops/backup_restore_drill.sh
```

It dumps the source, restores into the disposable database and fails unless the
Alembic revision and the `users`, `conversations`, `entries` and `tool_executions`
row counts match. Afterwards point a temporary backend at the restored database,
check `/health/ready`, verify a known test conversation by ID, then delete the
restore database and the dump.

A production restore stops writers, chooses the snapshot, restores into a new
database, runs the migration release step with the release image, verifies row
counts and readiness, and only then switches the database endpoint. Never
overwrite the live database in place.

## Observability and incidents

Collect JSON stdout/stderr centrally; access logs are disabled. Application events
carry an event name, request ID (free-form client IDs are logged as a digest),
route template, status, duration and fixed reason tokens — never a path, query
string, request body, transcript, audio, provider payload, exception message or
credential. Set `METRICS_ENABLED=true` to expose Prometheus metrics on `/metrics`;
the endpoint has no authentication, so the proxy must not route it publicly.
Alert on readiness failures, rising 5xx and 429 rates
(`mia_http_requests_total`, `mia_admission_rejections_total`), provider circuit
openings (`mia_provider_circuit_open`) and sustained latency
(`mia_http_request_duration_seconds`). If provider errors or spend escalate, use
the kill switches.

## Provider credential smoke

```sh
sh ops/credential_smoke.sh
```

Runs the credential-gated provider smokes against the real Gemini API. The
runtime image ships no test runner and `.dockerignore` keeps tests out of its
build context, so these tests previously had nowhere to execute: they skipped
from W3 onward while real provider defects accumulated. The script builds
`Dockerfile.test` — a verification image that is never deployed — and runs them
there. Docker reads the secret file; the script prints no credential.

Read the result carefully. A **skip** means the environment could not be
reached, not that the check passed: throttling, timeouts and provider outages
skip with the reason printed by `-rs`, because they say nothing about whether
MIA builds a request the API accepts. Anything else is a genuine rejection and
fails. Run it after changing a provider adapter, a tool declaration or a model,
and before a release.

Override the environment file with `MIA_ENV_FILE`, the models with
`GEMINI_TEXT_MODEL` / `GEMINI_LIVE_MODEL`, and the selection with
`MIA_SMOKE_TARGETS`; extra arguments are passed to pytest.

## Dependency and security scan

```sh
sh ops/security_scan.sh
```

Audits every locked dependency (`uv export` + `pip-audit`) and runs `bandit` over
`app`. CI runs the same script; rerun it locally whenever `uv.lock` changes and
treat any advisory as a release blocker until it is fixed or explicitly waived in
the session handoff.
