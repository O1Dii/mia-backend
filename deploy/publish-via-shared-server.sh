#!/usr/bin/env bash
# Publish Mia to the shared mindmapgen.xyz server through its existing VPN/SSH tool (M62-T1).
#   bash deploy/publish-via-shared-server.sh             full release of the bundle (build first)
#   bash deploy/publish-via-shared-server.sh --apk       only the APK of the bundle
#   bash deploy/publish-via-shared-server.sh --rollback  one step back on the server
# The owner authorized operational env loading, without inspecting secret files: the server
# password is loaded as a variable only, never printed or put on a command line.
set -euo pipefail
MODE=full
case ${1:-} in
  --apk) MODE=apk ;;
  --rollback) MODE=rollback ;;
  "") ;;
  *) echo 'usage: publish-via-shared-server.sh [--apk|--rollback]' >&2; exit 2 ;;
esac
MIA_SHARED_SCRIPT=${MIA_SHARED_SCRIPT:-/Users/aleksey/mind-map-generator/scripts/deploy.sh}
MIA_BUNDLE=${MIA_BUNDLE:-/tmp/mia-release}
MIA_REMOTE_BUNDLE=/tmp/mia-release-$(date +%s)-$$
case $MODE in
  full)
    SMALL=(mia.compose.yml mia.nginx.conf service.json release-mia.sh image.sha256)
    LARGE=(image.tar.gz)
    ;;
  apk)
    SMALL=(release-mia.sh)
    LARGE=(mia.apk)
    ;;
  rollback) SMALL=() LARGE=() ;;
esac
for artifact in ${SMALL[@]+"${SMALL[@]}"} ${LARGE[@]+"${LARGE[@]}"}; do
  [[ -f $MIA_BUNDLE/$artifact ]] || { echo "Build the bundle first: bash deploy/build-release.sh ($artifact missing)" >&2; exit 1; }
done
if [[ $MODE != rollback && -f $MIA_BUNDLE/mia.apk ]]; then
  if [[ $MODE == full ]]; then LARGE+=(mia.apk); fi
  if [[ -f $MIA_BUNDLE/mia.apk.json ]]; then SMALL+=(mia.apk.json); fi
fi
[[ -f $MIA_SHARED_SCRIPT ]]
if [[ -z ${SERVER_PASSWORD:-} ]]; then
  MIA_AUTH_ENV=${MIA_AUTH_ENV:-$(dirname "$MIA_SHARED_SCRIPT")/../deploy/server.env}
  [[ -f $MIA_AUTH_ENV ]] || { echo 'Export SERVER_PASSWORD through the approved environment loader.' >&2; exit 1; }
  # Load values as variables only. Never display the file or variable values.
  # Disable tracing; suppress diagnostics that might include source lines.
  set +x
  # shellcheck disable=SC1090
  if ! source "$MIA_AUTH_ENV" >/dev/null 2>&1; then
    echo 'Could not load the deployment environment.' >&2
    exit 1
  fi
fi
[[ -n ${SERVER_PASSWORD:-} ]] || { echo 'SERVER_PASSWORD is not available in the deployment environment.' >&2; exit 1; }
export SERVER_PASSWORD

if [[ $MODE == rollback ]]; then
  exec bash "$MIA_SHARED_SCRIPT" --ssh 'bash /opt/mia/release-mia.sh rollback'
fi

MIA_CONNECT_PID=
cleanup() {
  if [[ -n $MIA_CONNECT_PID ]] && kill -0 "$MIA_CONNECT_PID" 2>/dev/null; then
    kill "$MIA_CONNECT_PID" 2>/dev/null || true
    wait "$MIA_CONNECT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
# --ssh runs only the supplied command as root; it never deploys the neighboring app.
bash "$MIA_SHARED_SCRIPT" --ssh "
  install -d -m 700 -o user '$MIA_REMOTE_BUNDLE'
  for attempt in \$(seq 1 900); do
    if [[ -f '$MIA_REMOTE_BUNDLE/ready' ]]; then
      status=0
      bash '$MIA_REMOTE_BUNDLE/release-mia.sh' $MODE '$MIA_REMOTE_BUNDLE' || status=\$?
      rm -rf -- '$MIA_REMOTE_BUNDLE'
      exit \$status
    fi
    sleep 2
  done
  rm -rf -- '$MIA_REMOTE_BUNDLE'
  echo 'Release upload did not finish within 30 minutes.' >&2; exit 1
" &
MIA_CONNECT_PID=$!
MIA_SOCKET=/tmp/mindmapgen-deploy-$MIA_CONNECT_PID.sock
MIA_SSH=(ssh -o ControlMaster=no -o "ControlPath=$MIA_SOCKET" -o BatchMode=yes -o ConnectTimeout=5 -o ProxyCommand=none user@10.0.18.23)
MIA_CONNECTED=0
for _ in $(seq 1 120); do
  kill -0 "$MIA_CONNECT_PID" 2>/dev/null || { wait "$MIA_CONNECT_PID"; exit 1; }
  if [[ -S $MIA_SOCKET ]] && "${MIA_SSH[@]}" "test -d '$MIA_REMOTE_BUNDLE'" 2>/dev/null; then MIA_CONNECTED=1; break; fi
  sleep 1
done
[[ $MIA_CONNECTED == 1 ]]
for artifact in "${SMALL[@]}"; do
  "${MIA_SSH[@]}" "cat > '$MIA_REMOTE_BUNDLE/$artifact'" <"$MIA_BUNDLE/$artifact"
done
for artifact in "${LARGE[@]}"; do
  python3 "$(dirname "$MIA_SHARED_SCRIPT")/resumable_upload.py" \
    "$MIA_BUNDLE/$artifact" "$MIA_REMOTE_BUNDLE/$artifact" "${MIA_SSH[@]}"
done
"${MIA_SSH[@]}" "touch '$MIA_REMOTE_BUNDLE/ready'"
wait "$MIA_CONNECT_PID"
MIA_CONNECT_PID=
printf 'Verify https://mindmapgen.xyz/mia/ and the MIA tile on https://mindmapgen.xyz/\n'
