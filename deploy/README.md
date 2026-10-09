# Mia on the shared server

Test deployment of the MVP (M62-T1) at **https://mindmapgen.xyz/mia/**; hub card `mia`, order 40.
Not production: backups, restore drill and metrics come with M62. The full runbook, server
layout and smoke list are in [docs/DEPLOY_TEST.md](../docs/DEPLOY_TEST.md).

| File | Role |
|---|---|
| `../Dockerfile.web` | Release image: API + worker + migrations + web built with base `/mia` |
| `mia.compose.yml` | `/opt/mia/mia.compose.yml`: `mia` (API, 127.0.0.1:8004), `worker`, `postgres`, `redis` |
| `mia.nginx.conf` | `/etc/nginx/mindmapgen.d/mia.conf`: `/mia/` and the root well-known OAuth locations |
| `service.json` | Hub card, merged into `/srv/mindmapgen/services.json` |
| `build-release.sh` | Builds the bundle (amd64 image, files above, optional APK) in `$MIA_BUNDLE` |
| `publish-via-shared-server.sh` | Uploads the bundle over the shared VPN/SSH tool and runs `release-mia.sh` |
| `release-mia.sh` | On the server, as root: `full`, `apk` or `rollback` (automatic rollback on failure) |

```sh
MIA_APK=…/app-live-debug.apk MIA_APK_VERSION="0.1.0 (<android sha>)" bash deploy/build-release.sh
bash deploy/publish-via-shared-server.sh            # full release
bash deploy/publish-via-shared-server.sh --apk      # only a new APK (MIA_SKIP_IMAGE=1 to build)
bash deploy/publish-via-shared-server.sh --rollback # one step back
```

The server password is loaded by the shared deploy tool as an environment variable only; it is
never printed, copied into Mia or put on a command line. Server secrets (`/opt/mia/secrets/*.env`,
root 0600) are generated on the server on the first release and never leave it.

The WEB-P1 POC this replaced (`app.web_poc`, SQLite in `/opt/mia/data`) is described in the
WEB-P1-R2/R3 handoffs; its data is kept on the server untouched and was not migrated.
