#!/usr/bin/env bash
set -euo pipefail
MIA_REPO=$(cd "$(dirname "$0")/.." && pwd)
MIA_BUNDLE=${MIA_BUNDLE:-/tmp/mia-release}
mkdir -p "$MIA_BUNDLE"
docker buildx build --platform linux/amd64 -f "$MIA_REPO/Dockerfile.web" \
  --build-arg MIA_BASE_PATH=/mia -t mia:latest \
  -o "type=docker,dest=$MIA_BUNDLE/image.tar" "$MIA_REPO"
gzip -1 -f "$MIA_BUNDLE/image.tar"
for artifact in mia.compose.yml mia.nginx.conf service.json release-mia.sh; do
  cp "$MIA_REPO/deploy/$artifact" "$MIA_BUNDLE/$artifact"
done
printf 'Release bundle: %s\n' "$MIA_BUNDLE"
