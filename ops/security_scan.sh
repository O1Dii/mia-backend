#!/bin/sh
# Dependency and static security scan for the backend.
#
# Audits every locked dependency against the Python advisory databases and runs
# a static scan of application code. Run it from the repository root before a
# release and whenever the lockfile changes. It reads no secret file.
set -eu

pip_audit_version="${PIP_AUDIT_VERSION:-2.9.0}"
bandit_version="${BANDIT_VERSION:-1.9.4}"
requirements="$(mktemp)"
trap 'rm -f "$requirements"' EXIT

uv export --frozen --all-groups --no-hashes --no-emit-project >"$requirements"
uvx "pip-audit==$pip_audit_version" --requirement "$requirements" --no-deps --disable-pip
uvx "bandit==$bandit_version" --quiet --recursive app --severity-level medium
