#!/usr/bin/env bash
# M41 device gate (SM-S938B): «Напиши Яне Гейтовой в телеграм, что надо купить молоко» by voice.
#
# Reuses the M23 instrumentation test DeviceToolsVoiceGateTest#findsAContactByNameByVoice from the
# Android repo (it inserts the test contact «Яна Гейтова», grants READ_CONTACTS, injects the recorded
# phrase as microphone input, runs the REAL on-device STT, TTS and device tools). Only the phrase and
# the backend differ: gate_server.py (this directory) runs the real app from this worktree with a
# scripted model that calls people.resolve -> builtin.telegram.send_message, and approves the pending
# approval itself (the on-screen tap is a user check). NOTHING IS SENT: telegram.handoff only opens
# Telegram with a draft; this script never taps in Telegram and ends with BACK + HOME.
#
#   ANDROID_SERIAL=R5CY12AMHLY MIA_BACKEND=<this worktree> MIA_ANDROID=<mia-helper-android checkout> \
#   MIA_GATE_DATABASE_URL=postgresql+asyncpg://...  REDIS_URL=redis://localhost:56441/0 \
#   STT_MODEL_DIR=<dir with model.int8.onnx and tokens.txt of gigaam-v3-ctc>  GATE_PORT=8141 \
#   tests/plugins/telegram/device_gate/run_gate.sh
#
# The phone must be awake and UNLOCKED. Hold the shared device lock while this runs.
set -euo pipefail
SERIAL=${ANDROID_SERIAL:?set ANDROID_SERIAL}
BACKEND=${MIA_BACKEND:?path to this mia-helper worktree with .venv}
ANDROID=${MIA_ANDROID:?path to the mia-helper-android checkout (prebuilt devicetools androidTest APK)}
DB=${MIA_GATE_DATABASE_URL:?migrated, disposable Postgres database URL}
MODELS=${STT_MODEL_DIR:?directory with model.int8.onnx and tokens.txt}
PORT=${GATE_PORT:-8141}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${GATE_OUT:-$HERE/results}
WORK=$(mktemp -d)
REMOTE=/data/local/tmp/mia-m41
PKG=by.mia.assistant.core.devicetools.test
APK=$ANDROID/core/devicetools/build/outputs/apk/androidTest/debug/devicetools-debug-androidTest.apk
ADB=(adb -s "$SERIAL")  # a function wrapper breaks `|| true` under set -e in macOS bash 3.2
cleanup() {
  [ -n "${SERVER:-}" ] && kill "$SERVER" 2>/dev/null || true
  "${ADB[@]}" reverse --remove "tcp:$PORT" 2>/dev/null || true
  "${ADB[@]}" shell rm -rf "$REMOTE" || true
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/wavs" "$OUT"
say -v Milena -r 175 --file-format=WAVE --data-format=LEI16@16000 -o "$WORK/wavs/contacts.wav" \
  "Напиши Яне Гейтовой в телеграм, что надо купить молоко"

(cd "$BACKEND" && exec env PYTHONPATH=. DATABASE_URL="$DB" REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}" \
  GATE_PORT="$PORT" GATE_TOKEN_FILE="$WORK/token" .venv/bin/python "$HERE/gate_server.py") > "$WORK/server.log" 2>&1 &
SERVER=$!
for _ in $(seq 100); do [ -s "$WORK/token" ] && curl -sf "localhost:$PORT/health/live" >/dev/null && break; sleep 0.2; done
sleep 1.5  # plugin installation call

[ -f "$APK" ] || (cd "$ANDROID" && ./gradlew -q :core:devicetools:assembleDebugAndroidTest)
"${ADB[@]}" uninstall "$PKG" >/dev/null 2>&1 || true
"${ADB[@]}" install -r -t "$APK" >/dev/null
"${ADB[@]}" shell mkdir -p "$REMOTE/model" "$REMOTE/wavs"
"${ADB[@]}" push "$MODELS/model.int8.onnx" "$MODELS/tokens.txt" "$REMOTE/model/" >/dev/null
"${ADB[@]}" push "$WORK/wavs/." "$REMOTE/wavs/" >/dev/null
"${ADB[@]}" push "$WORK/token" "$REMOTE/token" >/dev/null
"${ADB[@]}" shell chmod 600 "$REMOTE/token"
"${ADB[@]}" reverse "tcp:$PORT" "tcp:$PORT" >/dev/null
"${ADB[@]}" shell input keyevent KEYCODE_WAKEUP
status=0
"${ADB[@]}" shell am instrument -w -e class by.mia.assistant.core.devicetools.DeviceToolsVoiceGateTest#findsAContactByNameByVoice \
  -e gateSource "$REMOTE" -e miaBaseUrl "http://127.0.0.1:$PORT" "$PKG/androidx.test.runner.AndroidJUnitRunner" | tee "$OUT/instrumentation.txt" || status=$?
"${ADB[@]}" shell dumpsys activity activities 2>/dev/null | grep -E "topResumedActivity" | sed 's/^ *//' > "$OUT/foreground.txt" || true
"${ADB[@]}" shell input keyevent KEYCODE_BACK
"${ADB[@]}" shell input keyevent KEYCODE_HOME
"${ADB[@]}" pull "/sdcard/Android/data/$PKG/files/m23-gate/contacts.json" "$OUT/contacts.json" >/dev/null || true
grep -E "^gate:" "$WORK/server.log" > "$OUT/backend-log.txt" || true
"${ADB[@]}" uninstall "$PKG" >/dev/null 2>&1 || true
echo "reports: $OUT"
exit $status
