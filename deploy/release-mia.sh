#!/usr/bin/env bash
# Run on the server as root after uploading the reviewable bundle to /tmp/mia-release.
# No secret files are read, copied or printed. Existing services are not restarted.
set -euo pipefail
MIA_BUNDLE=${1:-/tmp/mia-release}
MIA_ROOT=/opt/mia
MIA_PROXY=/etc/nginx/mindmapgen.d/mia.conf
MIA_CARD=/srv/mindmapgen/services.d/mia.json
if [[ ! -e "$MIA_ROOT/mia.compose.yml" ]] && ss -ltn '( sport = :8004 )' | tail -n +2 | read -r _; then
  echo 'Port 8004 is occupied; choose an unused port in the reviewed compose and nginx files.' >&2
  exit 1
fi
mkdir -p "$MIA_ROOT/data" /etc/nginx/mindmapgen.d /srv/mindmapgen/services.d
chown 10001:10001 "$MIA_ROOT/data"
chmod 700 "$MIA_ROOT/data"
MIA_BACKUP=$(mktemp -d /opt/mia-release-backup.XXXXXX)
for path in "$MIA_PROXY" "$MIA_CARD" "$MIA_ROOT/mia.compose.yml" /srv/mindmapgen/services.json; do
  if [[ -f "$path" ]]; then cp "$path" "$MIA_BACKUP/$(basename "$path")"; fi
done
MIA_HAD_IMAGE=0
if docker image inspect mia:latest >/dev/null 2>&1; then
  docker tag mia:latest mia:previous
  MIA_HAD_IMAGE=1
fi
rollback() {
  trap - ERR
  for target in "$MIA_PROXY" "$MIA_CARD" /srv/mindmapgen/services.json; do
    if [[ -f "$MIA_BACKUP/$(basename "$target")" ]]; then
      cp "$MIA_BACKUP/$(basename "$target")" "$target"
    else rm -f "$target"; fi
  done
  if [[ $MIA_HAD_IMAGE == 1 ]]; then
    docker tag mia:previous mia:latest
    if [[ -f "$MIA_BACKUP/mia.compose.yml" ]]; then cp "$MIA_BACKUP/mia.compose.yml" "$MIA_ROOT/mia.compose.yml"; fi
    (cd "$MIA_ROOT" && docker compose -p mia -f mia.compose.yml up -d --force-recreate)
  else
    (cd "$MIA_ROOT" && docker compose -p mia -f mia.compose.yml down) || true
  fi
  nginx -t && systemctl reload nginx
  echo 'MIA release rolled back; other services were not restarted.' >&2
  exit 1
}
trap rollback ERR
gunzip -c "$MIA_BUNDLE/image.tar.gz" | docker load
cp "$MIA_BUNDLE/mia.compose.yml" "$MIA_ROOT/mia.compose.yml"
(cd "$MIA_ROOT" && docker compose -p mia -f mia.compose.yml up -d --force-recreate)
MIA_READY=0
for attempt in $(seq 1 45); do
  if curl -fsS -o /dev/null http://127.0.0.1:8004/api/web/health; then MIA_READY=1; break; fi
  sleep 2
done
[[ $MIA_READY == 1 ]]
# Guard against accidentally publishing an anonymous development server.
curl -fsS http://127.0.0.1:8004/api/web/auth/session | python3 -c 'import sys,json; v=json.load(sys.stdin); assert v["required"] and v["google_hub"] and v["user"] is None'
[[ $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8004/api/web/workspace) == 401 ]]
cp "$MIA_BUNDLE/mia.nginx.conf" "$MIA_PROXY"
nginx -t
systemctl reload nginx
cp "$MIA_BUNDLE/service.json" "$MIA_CARD"
python3 - <<'PY'
import json, pathlib
root=pathlib.Path('/srv/mindmapgen')
cards=[json.loads(p.read_text()) for p in sorted((root/'services.d').glob('*.json'))]
cards.sort(key=lambda c:(c.get('order',100),c.get('title','')))
file=root/'services.json.tmp'
file.write_text(json.dumps(cards,ensure_ascii=False,indent=2)+'\n')
file.replace(root/'services.json')
PY
# During nginx reload, an old worker can briefly return the hub's HTML with 200.
# Validate the JSON payload rather than treating any successful status as MIA.
MIA_PROXY_READY=0
for attempt in $(seq 1 20); do
  if curl -fsS -H 'Host: mindmapgen.xyz' http://127.0.0.1/mia/api/web/auth/session \
    | python3 -c 'import sys,json; v=json.load(sys.stdin); assert v["required"] and v["google_hub"] and v["user"] is None' 2>/dev/null; then
    MIA_PROXY_READY=1; break
  fi
  sleep 1
done
[[ $MIA_PROXY_READY == 1 ]]
curl -fsS -H 'Host: mindmapgen.xyz' http://127.0.0.1/mia/api/web/health
trap - ERR
printf '\nPublished MIA at https://mindmapgen.xyz/mia/\n'
