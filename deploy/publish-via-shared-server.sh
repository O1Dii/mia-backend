#!/usr/bin/env bash
# Uses the existing server's authentication script and environment variables.
# The owner authorized operational env loading, without inspecting secret files.
set -euo pipefail
MIA_REPO=$(cd "$(dirname "$0")/.." && pwd)
MIA_SHARED_SCRIPT=${MIA_SHARED_SCRIPT:-/Users/aleksey/mind-map-generator/scripts/deploy.sh}
MIA_BUNDLE=${MIA_BUNDLE:-/tmp/mia-release}
MIA_REMOTE_BUNDLE=/tmp/mia-release-$(date +%s)-$$
for artifact in image.tar.gz mia.compose.yml mia.nginx.conf service.json release-mia.sh; do
  [[ -f "$MIA_BUNDLE/$artifact" ]] || { echo "Build the release bundle first: bash deploy/build-web.sh" >&2; exit 1; }
done
[[ -f "$MIA_SHARED_SCRIPT" ]]
if [[ -z ${SERVER_PASSWORD:-} ]]; then
  MIA_AUTH_ENV=${MIA_AUTH_ENV:-$(dirname "$MIA_SHARED_SCRIPT")/../deploy/server.env}
  [[ -f "$MIA_AUTH_ENV" ]] || { echo 'Export SERVER_PASSWORD through the approved environment loader.' >&2; exit 1; }
  # Load values as variables only. Never display the file or variable values.
  # Disable tracing; suppress diagnostics that might include source lines.
  set +x
  if ! source "$MIA_AUTH_ENV" >/dev/null 2>&1; then
    echo 'Could not load the deployment environment.' >&2; exit 1
  fi
fi
[[ -n ${SERVER_PASSWORD:-} ]] || { echo 'SERVER_PASSWORD is not available in the deployment environment.' >&2; exit 1; }
export SERVER_PASSWORD
MIA_CONNECT_PID=
cleanup() {
  if [[ -n "$MIA_CONNECT_PID" ]] && kill -0 "$MIA_CONNECT_PID" 2>/dev/null; then
    kill "$MIA_CONNECT_PID" 2>/dev/null || true
    wait "$MIA_CONNECT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
# --ssh runs only the supplied command; it never deploys the neighboring app.
bash "$MIA_SHARED_SCRIPT" --ssh "
  if [[ ! -e /opt/mia/mia.compose.yml ]] && ss -ltn '( sport = :8004 )' | tail -n +2 | read -r _; then
    echo 'Port 8004 is occupied; deployment stopped.' >&2; exit 1
  fi
  docker ps --format '{{.Names}} {{.Status}}'
  install -d -m 700 -o user '$MIA_REMOTE_BUNDLE'
  for attempt in \$(seq 1 600); do
    if [[ -f '$MIA_REMOTE_BUNDLE/ready' ]]; then
      bash '$MIA_REMOTE_BUNDLE/release-mia.sh' '$MIA_REMOTE_BUNDLE'
      rm -rf -- '$MIA_REMOTE_BUNDLE'
      exit 0
    fi
    sleep 2
  done
  echo 'Release upload did not finish within 20 minutes.' >&2; exit 1
" &
MIA_CONNECT_PID=$!
MIA_SOCKET=/tmp/mindmapgen-deploy-$MIA_CONNECT_PID.sock
MIA_SSH=(ssh -o ControlMaster=no -o "ControlPath=$MIA_SOCKET" -o BatchMode=yes -o ConnectTimeout=5 -o ProxyCommand=none user@10.0.18.23)
MIA_CONNECTED=0
for attempt in $(seq 1 120); do
  kill -0 "$MIA_CONNECT_PID" 2>/dev/null || { wait "$MIA_CONNECT_PID"; exit 1; }
  if [[ -S "$MIA_SOCKET" ]] && "${MIA_SSH[@]}" "test -d '$MIA_REMOTE_BUNDLE'" 2>/dev/null; then MIA_CONNECTED=1; break; fi
  sleep 1
done
[[ $MIA_CONNECTED == 1 ]]
for artifact in mia.compose.yml mia.nginx.conf service.json release-mia.sh; do
  "${MIA_SSH[@]}" "cat > '$MIA_REMOTE_BUNDLE/$artifact'" < "$MIA_BUNDLE/$artifact"
done
python3 "$(dirname "$MIA_SHARED_SCRIPT")/resumable_upload.py" \
  "$MIA_BUNDLE/image.tar.gz" "$MIA_REMOTE_BUNDLE/image.tar.gz" "${MIA_SSH[@]}"
"${MIA_SSH[@]}" "touch '$MIA_REMOTE_BUNDLE/ready'"
wait "$MIA_CONNECT_PID"
MIA_CONNECT_PID=
printf 'Verify https://mindmapgen.xyz/mia/ and the MIA tile on https://mindmapgen.xyz/\n'
