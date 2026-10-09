#!/usr/bin/env bash
# Build the reviewable release bundle of the hosted Mia (M62-T1) in $MIA_BUNDLE:
# amd64 image (API + worker + migrations + web under /mia), compose, nginx location, hub card,
# server release script and, when MIA_APK points at a built APK, the Android download.
#   MIA_APK=/path/app-live-debug.apk MIA_APK_VERSION="0.1.0 (c23e2e5)" bash deploy/build-release.sh
# Nothing secret goes into the bundle: the Google Web client ID is public.
set -euo pipefail
MIA_REPO=$(cd "$(dirname "$0")/.." && pwd)
MIA_BUNDLE=${MIA_BUNDLE:-/tmp/mia-release}
MIA_GOOGLE_CLIENT_ID=${MIA_GOOGLE_CLIENT_ID:-806288812204-oobsnndkd9unnmn2rclisebkfnt8n63b.apps.googleusercontent.com}
mkdir -p "$MIA_BUNDLE"
if [[ ${MIA_SKIP_IMAGE:-0} != 1 ]]; then
  docker buildx build --platform linux/amd64 -f "$MIA_REPO/Dockerfile.web" \
    --build-arg MIA_BASE_PATH=/mia --build-arg VITE_GOOGLE_CLIENT_ID="$MIA_GOOGLE_CLIENT_ID" \
    -t mia:latest -o "type=docker,dest=$MIA_BUNDLE/image.tar" "$MIA_REPO"
  gzip -1 -f "$MIA_BUNDLE/image.tar"
  (cd "$MIA_BUNDLE" && shasum -a 256 image.tar.gz >image.sha256)
fi
for artifact in mia.compose.yml mia.nginx.conf service.json release-mia.sh; do
  cp "$MIA_REPO/deploy/$artifact" "$MIA_BUNDLE/$artifact"
done
if [[ -n ${MIA_APK:-} ]]; then
  cp "$MIA_APK" "$MIA_BUNDLE/mia.apk"
  python3 - "$MIA_BUNDLE/mia.apk.json" "${MIA_APK_VERSION:-}" "${MIA_APK_VERSION_CODE:-1}" <<'PY'
import json, sys
path, name, code = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(path, "w") as out:
    json.dump({"version_name": name or None, "version_code": code}, out)
PY
fi
printf 'Release bundle: %s\n' "$MIA_BUNDLE"
