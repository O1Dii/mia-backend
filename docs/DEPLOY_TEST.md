# Test deployment: https://mindmapgen.xyz/mia/ (M62-T1)

The MVP (API, worker, PostgreSQL, Redis, web) on the shared mindmapgen.xyz VM, replacing the
WEB-P1 POC. It is a test stand for the owner, not production: no off-VM backups, no restore drill,
no metrics (M62). Handoff: [M62-T1](implementation/handoffs/M62-T1.md).

## Topology

```
browser / Android ── https ── provider TLS gateway (10.0.18.240) ── http :80 nginx (host)
   /mia/…                    → 127.0.0.1:8004 (prefix stripped) → container `mia` (uvicorn app.main)
   /.well-known/…/mia[/mcp]  → the same API (RFC 8414 / RFC 9728 locations of a path issuer)
container `mia_worker` (python -m app.worker), `mia_postgres` (16), `mia_redis` (7, no persistence)
```

One image (`Dockerfile.web`) holds the API, the worker, Alembic migrations, the plugin contracts and
the web bundle built with `MIA_BASE_PATH=/mia` and the public Google Web client ID. The API serves
the web from `WEB_DIST_DIR` (SPA fallback; `/v1`, `/oauth`, `/.well-known`, `/mcp`, `/health`,
`/downloads` never fall back to HTML) and the APK from `ANDROID_APK_PATH`.

`PUBLIC_BASE_URL=https://mindmapgen.xyz/mia` drives everything path-dependent: web cookies
`Path=/mia/` (`Secure`, `HttpOnly`, `SameSite=Strict`), the WebView hand-off `redeem_path`
(`/mia/v1/auth/web/handoff/redeem?…`) and its landing redirect, the plugin OAuth callback
(`https://mindmapgen.xyz/mia/v1/connections/callback`), its binding cookie and return redirect, the
OAuth issuer of «Войти через Мию» and the consent page `…/mia/oauth.html`.

## Server layout (only these paths belong to Mia)

| Path | Content |
|---|---|
| `/opt/mia/mia.compose.yml` | compose of the current release (project `mia`) |
| `/opt/mia/secrets/{app,postgres,redis}.env` | generated once on the server, root 0600 |
| `/opt/mia/postgres/` | PostgreSQL data |
| `/opt/mia/releases/mia.apk`, `mia.apk.json` | Android download and its version (read-only mount) |
| `/opt/mia/backups/<UTC>/`, `backups/last` | previous compose, nginx location, card, image id |
| `/opt/mia/release-mia.sh` | release script of the current release (for `rollback`) |
| `/opt/mia/data/` | WEB-P1 POC SQLite, kept untouched as a backup (not migrated) |
| `/etc/nginx/mindmapgen.d/mia.conf` | nginx locations |
| `/srv/mindmapgen/services.d/mia.json` | hub card |

Environment names: compose sets `APP_ENV=production`, `PUBLIC_BASE_URL`, `GOOGLE_CLIENT_ID`,
`CHATGPT_PLAN_LOOPBACK_LISTENER=false`, `WEB_DIST_DIR`, `ANDROID_APK_PATH`, `FORWARDED_ALLOW_IPS`,
`STORE_PACKAGES_DIR`; `secrets/app.env` holds `DATABASE_URL`, `REDIS_URL`, `AUTH_JWT_SECRET`,
`CREDENTIALS_MASTER_KEYS`, `CREDENTIALS_MASTER_KEY_ACTIVE`; `postgres.env` `POSTGRES_PASSWORD`;
`redis.env` `REDIS_PASSWORD`. Losing `secrets/` makes stored model keys unreadable: copy it with
any manual backup of `/opt/mia/postgres`.

Memory limits (2 GB VM, four apps): API 384m, worker 256m, PostgreSQL 192m (shared_buffers 32MB,
50 connections), Redis 64m (maxmemory 32mb). Measured idle use ≈ 300 MiB for all four.

## Release

```sh
# Android build (A repo, detached worktree, no code changes):
./gradlew :app:assembleLiveDebug -PmiaBackendBaseUrl=https://mindmapgen.xyz/mia \
  -PmiaWebBaseUrl=https://mindmapgen.xyz/mia -PmiaWebClientId=<public Web client ID>
# Backend repo:
MIA_APK=<apk> MIA_APK_VERSION="0.1.0 (<sha>)" bash deploy/build-release.sh
bash deploy/publish-via-shared-server.sh
```

`release-mia.sh full` checks the image checksum, snapshots compose/nginx/card and tags the running
image `mia:previous`, loads the new image, creates secrets on first run, starts PostgreSQL and
Redis, runs `alembic upgrade head` with the new image, recreates `mia` and `worker`, then asserts
`/health/ready` 200, anonymous `/v1/feed` 401, the `/mia/` web index, the OAuth issuer, the APK route
(200/404) and a running worker; installs the nginx location (`nginx -t`, reload) and the card, and
repeats the checks through nginx. Any failure restores the snapshot and the previous image.

New APK only: `MIA_SKIP_IMAGE=1 MIA_APK=<apk> MIA_APK_VERSION=… bash deploy/build-release.sh`,
then `bash deploy/publish-via-shared-server.sh --apk`.

Rollback (one step): `bash deploy/publish-via-shared-server.sh --rollback` (runs
`/opt/mia/release-mia.sh rollback`). Going back to the POC removes the worker/PostgreSQL/Redis
containers; their data stays in `/opt/mia`. Migrations are forward-only: rolling an MVP release
back to an older MVP image after a schema change needs a matching database.

## Smoke after a release

```sh
curl -s https://mindmapgen.xyz/mia/health/ready                     # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}' https://mindmapgen.xyz/mia/v1/feed   # 401
curl -s https://mindmapgen.xyz/.well-known/oauth-authorization-server/mia   # issuer …/mia
curl -sI https://mindmapgen.xyz/mia/downloads/mia.apk               # 200, attachment, no-cache
```

Plus: other apps (`/`, `/source-refiner/`, `/knowly/`) unchanged; web cookies carry `Path=/mia/`.

## ChatGPT subscription on a hosted server

OpenAI's agent sign-in accepts only a `http://127.0.0.1:<port>/auth/callback` redirect and has no
device-code grant (checked 2026-10-10, developers.openai.com/siwc). «Модели» → «ChatGPT (подписка)»
opens the ChatGPT sign-in in a new tab; after consent that tab lands on an address that does not
load. The user copies it into Mia; the backend checks `state` and exchanges the code with the same
PKCE verifier (`POST /v1/model-connections/chatgpt-plan/sign-in/{id}/callback`). A backend on the
user's own machine (`CHATGPT_PLAN_LOOPBACK_LISTENER=true`, the default) still completes by itself.

## Publishing builtin plugins on the server

The image has no `cli/` and `contracts/` (OI-M15-004), so publication runs in a one-off container of the
same image with both directories mounted read-only (done 2026-10-10 for the first admin):

```sh
# Mac: pack without macOS metadata (AppleDouble `._*` files break schema loading)
COPYFILE_DISABLE=1 tar --no-xattrs --exclude=__pycache__ --exclude='._*' -czf mia-pub.tgz contracts/mvp cli
# copy to /tmp/mia-pub on the server (shared deploy script --ssh), then on the server:
docker compose -p mia --project-directory /opt/mia -f /opt/mia/mia.compose.yml run --rm --no-deps -T \
  -e STORE_ADMIN_USER_IDS=<users.id> -v /tmp/mia-pub/cli:/app/cli:ro -v /tmp/mia-pub/contracts:/app/contracts:ro \
  mia python -m cli.mia_plugin publish-builtin --as-user <users.id>
rm -rf /tmp/mia-pub
```

## Telegram bot on the server (2026-10-10)

`/opt/mia/secrets/app.env` holds `TELEGRAM_BOT_TOKEN` (copied from the owner's local `.env` with permission,
never printed), `TELEGRAM_BOT_USERNAME=mia_helper_ai_bot`, `TELEGRAM_WEBHOOK_SECRET` (generated on the server).
After editing: `docker compose -p mia --project-directory /opt/mia -f /opt/mia/mia.compose.yml up -d
--force-recreate --no-deps mia worker`, then `… exec -T mia python -m app.modules.notifications.telegram_webhook set`.
Webhook: `https://mindmapgen.xyz/mia/webhooks/telegram` (checked with `getWebhookInfo`, no errors).
