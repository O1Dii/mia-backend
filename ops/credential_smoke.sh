#!/bin/sh
# Run the credential-gated provider smokes against real Gemini.
#
# The runtime image ships no test runner on purpose, so these tests had nowhere
# to execute and silently skipped from W3 onward while real provider defects
# accumulated. This builds `Dockerfile.test` and runs them there.
#
# Docker reads the secret file; nothing here prints a credential. A smoke that
# SKIPS means the environment carries no key -- that is not a pass.
#
#   sh ops/credential_smoke.sh
#   MIA_ENV_FILE=deploy.env sh ops/credential_smoke.sh
#   MIA_SMOKE_TARGETS=tests/providers/test_gemini_text_smoke.py sh ops/credential_smoke.sh
set -eu

ENV_FILE="${MIA_ENV_FILE:-.env}"
IMAGE="${MIA_TEST_IMAGE:-mia-backend-test:local}"

if [ ! -f "$ENV_FILE" ]; then
    echo "No environment file at $ENV_FILE; set MIA_ENV_FILE." >&2
    exit 2
fi

# Model names are public build inputs, not secrets. The smokes skip without
# them, so default to the families the adapters are actually verified against:
# Gemini 3.x rejects the manual tool loop, and only 2.5 native audio works Live.
GEMINI_TEXT_MODEL="${GEMINI_TEXT_MODEL:-gemini-2.5-flash}"
GEMINI_LIVE_MODEL="${GEMINI_LIVE_MODEL:-gemini-2.5-flash-native-audio-latest}"
export GEMINI_TEXT_MODEL GEMINI_LIVE_MODEL

# Arguments are extra pytest flags; override the selection with MIA_SMOKE_TARGETS
# so a flag never silently replaces the tests being run.
TARGETS="${MIA_SMOKE_TARGETS:-tests/providers/test_gemini_live_smoke.py tests/providers/test_gemini_text_smoke.py}"

docker build -f Dockerfile.test -t "$IMAGE" .

# `-e VAR` without a value forwards this shell's value and overrides the file,
# so the caller can pin a model without editing secrets.
docker run --rm \
    --env-file "$ENV_FILE" \
    -e GEMINI_TEXT_MODEL \
    -e GEMINI_LIVE_MODEL \
    "$IMAGE" \
    pytest -q -rs -p no:cacheprovider "$@" $TARGETS
