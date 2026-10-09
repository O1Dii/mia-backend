#!/usr/bin/env bash
# Mia release on the shared mindmapgen.xyz server (M62-T1). Run as root by
# deploy/publish-via-shared-server.sh, or by hand from /opt/mia/release-mia.sh:
#   release-mia.sh full <bundle>   image, compose, migrations, nginx location, hub card (+ APK)
#   release-mia.sh apk <bundle>    only the APK offered at /mia/downloads/mia.apk
#   release-mia.sh rollback        previous image, compose, nginx location and card (one step)
# Touches only /opt/mia/**, /etc/nginx/mindmapgen.d/mia.conf and the mia hub card; other apps
# are never restarted (nginx is reloaded). Secrets are generated here once and never printed.
set -Eeuo pipefail
umask 077
MODE=${1:?usage: release-mia.sh full|apk <bundle> | rollback}
BUNDLE=${2:-}
ROOT=/opt/mia
PROXY=/etc/nginx/mindmapgen.d/mia.conf
HUB=/srv/mindmapgen
CARD=$HUB/services.d/mia.json
ISSUER=https://mindmapgen.xyz/mia
LOCAL=http://127.0.0.1:8004
COMPOSE=(docker compose -p mia --project-directory "$ROOT" -f "$ROOT/mia.compose.yml")
BACKUP=

log() { printf '==> %s\n' "$*"; }
fail() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}
code_of() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }
via_proxy() { curl -fsS --max-time 10 -H 'Host: mindmapgen.xyz' "http://127.0.0.1$1"; }

merge_cards() {
  python3 - "$HUB" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
cards = [json.loads(p.read_text()) for p in sorted((root / "services.d").glob("*.json"))]
cards.sort(key=lambda c: (c.get("order", 100), c.get("title", "")))
tmp = root / "services.json.tmp"
tmp.write_text(json.dumps(cards, ensure_ascii=False, indent=2) + "\n")
tmp.chmod(0o644)
tmp.replace(root / "services.json")
PY
}

wait_healthy() {
  local name=$1 state=
  for _ in $(seq 1 60); do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || true)
    [[ $state == healthy ]] && return 0
    sleep 2
  done
  fail "$name is not healthy ($state)"
}

ensure_secrets() {
  local dir=$ROOT/secrets have=0
  install -d -m 700 -o root -g root "$dir"
  for name in app postgres redis; do
    if [[ -f $dir/$name.env ]]; then have=$((have + 1)); fi
  done
  if [[ $have == 3 ]]; then return 0; fi
  [[ $have == 0 ]] || fail "$dir is incomplete; restore it from a backup"
  if [[ -d $ROOT/postgres ]] && [[ -n $(ls -A "$ROOT/postgres") ]]; then
    fail "$ROOT/postgres has data but no secrets; restore $dir from a backup"
  fi
  python3 - "$dir" <<'PY'
import base64, os, secrets, sys
folder = sys.argv[1]
pg, rd = secrets.token_hex(24), secrets.token_hex(24)
master = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
files = {
    "postgres.env": f"POSTGRES_PASSWORD={pg}\n",
    "redis.env": f"REDIS_PASSWORD={rd}\n",
    "app.env": (
        f"DATABASE_URL=postgresql+asyncpg://mia:{pg}@postgres:5432/mia\n"
        f"REDIS_URL=redis://:{rd}@redis:6379/0\n"
        f"AUTH_JWT_SECRET={secrets.token_urlsafe(48)}\n"
        f"CREDENTIALS_MASTER_KEYS=k1:{master}\n"
        "CREDENTIALS_MASTER_KEY_ACTIVE=k1\n"
    ),
}
for name, body in files.items():
    fd = os.open(os.path.join(folder, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(body)
PY
  log "Generated server secrets in $dir (values not shown)"
}

install_apk() {
  [[ -f $BUNDLE/mia.apk ]] || return 0
  install -d -m 755 "$ROOT/releases"
  install -m 644 "$BUNDLE/mia.apk" "$ROOT/releases/.mia.apk.new"
  if [[ -f $BUNDLE/mia.apk.json ]]; then
    install -m 644 "$BUNDLE/mia.apk.json" "$ROOT/releases/.mia.apk.json.new"
    mv -f "$ROOT/releases/.mia.apk.json.new" "$ROOT/releases/mia.apk.json"
  fi
  mv -f "$ROOT/releases/.mia.apk.new" "$ROOT/releases/mia.apk"
  log "APK published: $(stat -c %s "$ROOT/releases/mia.apk") bytes"
}

snapshot() {
  BACKUP=$ROOT/backups/$(date -u +%Y%m%dT%H%M%SZ)
  install -d -m 700 "$ROOT/backups" "$BACKUP"
  if [[ -f $ROOT/mia.compose.yml ]]; then cp -p "$ROOT/mia.compose.yml" "$BACKUP/compose.yml"; fi
  if [[ -f $PROXY ]]; then cp -p "$PROXY" "$BACKUP/nginx.conf"; fi
  if [[ -f $CARD ]]; then cp -p "$CARD" "$BACKUP/card.json"; fi
  if docker image inspect mia:latest >/dev/null 2>&1; then
    docker tag mia:latest mia:previous
    docker image inspect -f '{{.Id}}' mia:previous >"$BACKUP/image.id"
  fi
  ln -sfn "$BACKUP" "$ROOT/backups/last"
}

restore() {
  local from=$1
  log "Rolling back to $from"
  if [[ -f $from/nginx.conf ]]; then cp "$from/nginx.conf" "$PROXY"; else rm -f "$PROXY"; fi
  if [[ -f $from/card.json ]]; then cp "$from/card.json" "$CARD"; else rm -f "$CARD"; fi
  merge_cards
  if [[ -f $from/image.id ]] && docker image inspect mia:previous >/dev/null 2>&1; then
    docker tag mia:previous mia:latest
  fi
  if [[ -f $from/compose.yml ]]; then
    cp "$from/compose.yml" "$ROOT/mia.compose.yml"
    # Services the previous compose does not name (e.g. worker, postgres, redis when going back
    # to the POC) are removed; their data directories stay in /opt/mia.
    "${COMPOSE[@]}" up -d --force-recreate --remove-orphans
  else
    "${COMPOSE[@]}" down --remove-orphans || true
  fi
  nginx -t && systemctl reload nginx
}

on_error() {
  trap - ERR
  printf 'Release failed; restoring the previous MIA.\n' >&2
  if [[ -n $BACKUP ]]; then restore "$BACKUP" || true; fi
  printf 'MIA rolled back; other services were not restarted.\n' >&2
  exit 1
}

check_local() {
  local ready=0
  for _ in $(seq 1 45); do
    if [[ $(code_of "$LOCAL/health/ready") == 200 ]]; then ready=1 && break; fi
    sleep 2
  done
  [[ $ready == 1 ]] || fail "API is not ready"
  [[ $(code_of "$LOCAL/v1/feed") == 401 ]] || fail "anonymous /v1/feed must be 401"
  local page
  page=$(curl -fsS --max-time 10 "$LOCAL/") || fail "web index"
  [[ $page == *'src="/mia/assets/'* ]] || fail "web index is not the /mia build"
  curl -fsS --max-time 10 "$LOCAL/.well-known/oauth-authorization-server" |
    python3 -c 'import json,sys; assert json.load(sys.stdin)["issuer"] == sys.argv[1]' "$ISSUER" ||
    fail "OAuth issuer is wrong"
  local apk
  apk=$(code_of -I "$LOCAL/downloads/mia.apk")
  [[ $apk == 200 || $apk == 404 ]] || fail "APK route answered $apk"
  sleep 5
  [[ $(docker inspect -f '{{.State.Running}}/{{.RestartCount}}' mia_worker) == true/0 ]] ||
    fail "worker is not running"
}

check_proxy() {
  local ok=0
  # During a reload an old nginx worker can still answer with the hub page: check the JSON.
  for _ in $(seq 1 20); do
    if via_proxy /mia/health/ready 2>/dev/null |
      python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"' 2>/dev/null; then
      ok=1 && break
    fi
    sleep 1
  done
  [[ $ok == 1 ]] || fail "/mia/health/ready through nginx"
  via_proxy /.well-known/oauth-authorization-server/mia |
    python3 -c 'import json,sys; assert json.load(sys.stdin)["issuer"] == sys.argv[1]' "$ISSUER" ||
    fail "root well-known metadata"
  via_proxy /.well-known/oauth-protected-resource/mia/mcp |
    python3 -c 'import json,sys; assert json.load(sys.stdin)["resource"] == sys.argv[1] + "/mcp"' "$ISSUER" ||
    fail "MCP resource metadata"
  [[ $(code_of -H 'Host: mindmapgen.xyz' http://127.0.0.1/mia/v1/feed) == 401 ]] ||
    fail "/mia/v1/feed through nginx must be 401"
  local page
  page=$(via_proxy /mia/) || fail "/mia/ through nginx"
  [[ $page == *'src="/mia/assets/'* ]] || fail "/mia/ through nginx is not the MIA web"
}

release_full() {
  [[ -d $BUNDLE ]] || fail "no bundle at $BUNDLE"
  (cd "$BUNDLE" && sha256sum -c --quiet image.sha256) || fail "image checksum mismatch"
  if ss -ltn '( sport = :8004 )' | tail -n +2 | grep -q . &&
    ! docker ps --format '{{.Names}}' | grep -qx mia; then
    fail "port 8004 is used by something other than MIA"
  fi
  snapshot
  trap on_error ERR
  log "Loading the image"
  gunzip -c "$BUNDLE/image.tar.gz" | docker load
  docker image inspect postgres:16-alpine >/dev/null 2>&1 || docker pull -q postgres:16-alpine
  docker image inspect redis:7-alpine >/dev/null 2>&1 || docker pull -q redis:7-alpine
  ensure_secrets
  install -d -m 700 "$ROOT/postgres"
  install -d -m 755 "$ROOT/releases"
  install_apk
  install -m 644 "$BUNDLE/mia.compose.yml" "$ROOT/mia.compose.yml"
  install -m 700 "$BUNDLE/release-mia.sh" "$ROOT/release-mia.sh"
  log "Starting PostgreSQL and Redis"
  "${COMPOSE[@]}" up -d postgres redis
  wait_healthy mia_postgres
  wait_healthy mia_redis
  log "Migrating the database"
  "${COMPOSE[@]}" run --rm --no-deps mia alembic upgrade head
  log "Starting the API and the worker"
  "${COMPOSE[@]}" up -d --force-recreate --remove-orphans mia worker
  check_local
  log "Installing the nginx location and the hub card"
  install -m 644 "$BUNDLE/mia.nginx.conf" "$PROXY"
  nginx -t
  systemctl reload nginx
  install -d -m 755 "$HUB/services.d"
  install -m 644 "$BUNDLE/service.json" "$CARD"
  merge_cards
  check_proxy
  trap - ERR
  log "Released image $(docker image inspect -f '{{.Id}}' mia:latest)"
  docker ps --filter name=mia --format '{{.Names}} {{.Status}}'
}

release_apk() {
  [[ -f $BUNDLE/mia.apk ]] || fail "no APK in $BUNDLE"
  install_apk
  [[ $(code_of -I "$LOCAL/downloads/mia.apk") == 200 ]] || fail "APK is not served"
}

case $MODE in
  full) release_full ;;
  apk) release_apk ;;
  rollback)
    [[ -e $ROOT/backups/last ]] || fail "no previous release recorded"
    restore "$(readlink -f "$ROOT/backups/last")"
    ;;
  *) fail "unknown mode $MODE" ;;
esac
