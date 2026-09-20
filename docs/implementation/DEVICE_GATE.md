# Running a physical-device gate

Everything a session needs to put the Android app in front of a real backend and
a real provider. W6 rediscovered these steps several times; they live here so the
next one does not.

Nothing in this file requires a credential value. Docker reads `.env`; the agent
running the gate never opens it.

## Why a device gate is not optional

Every defect W6 found was invisible without real cloud traffic, and several were
invisible without a real device. A green deterministic suite says nothing about
whether Gemini accepts what MIA sends: at one point the whole suite passed while
every real text turn and every voice session failed. Run against
`AI_PROVIDER=gemini`, never against the fake provider, when the point is to
judge the product.

## 1. Backend with the real provider

```sh
MIA_BACKEND_PORT=8088 docker compose \
  -f docker-compose.yml -f ops/live.compose.yml up -d --build
curl --fail --silent http://127.0.0.1:8088/health/ready
```

`ops/live.compose.yml` supplies `.env` to the container and pins the provider
models. Pick a port nothing else holds; 8000 is commonly taken.

The pinned models are not arbitrary:

| Variable | Value | Why |
| --- | --- | --- |
| `GEMINI_TEXT_MODEL` | `gemini-2.5-flash` | Every 3.x text model rejects the tool continuation turn ([S13-R1](handoffs/S13-R1.md)). |
| `GEMINI_LIVE_MODEL` | `gemini-2.5-flash-native-audio-latest` | `gemini-3.8-live` connects and never answers; `gemini-3.1-flash-live-preview` will not connect ([OI-W6-004](OPEN_ISSUES.md)). |

## 2. Reaching it from the phone

Use `adb reverse`, not a public tunnel: it needs no external ingress, and debug
builds carry a loopback-only cleartext policy for exactly this.

```sh
adb devices -l                      # must print `device`, not `unauthorized`
adb reverse tcp:8088 tcp:8088
adb shell 'curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8088/health/ready'
```

`unauthorized` means the phone is showing the RSA prompt: unlock it and accept.
`adb kill-server` then re-running `adb devices` re-triggers the prompt.

## 3. Installing the live build

```sh
JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home \
ANDROID_HOME=$HOME/Library/Android/sdk \
./gradlew :app:installLiveDebug \
  -PmiaBackendBaseUrl=http://127.0.0.1:8088 \
  -PmiaWebClientId=<web oauth client id>
```

`miaWebClientId` is the Google **Web** OAuth client ID and must equal the
backend's `GOOGLE_CLIENT_ID`. It is a public build input, not a secret; it is
also recoverable from a previously built APK. The `mock` flavour has no backend
session and no voice: use `liveDebug`.

## 4. Keeping the screen alive

A gate longer than the screen timeout dies with it.

```sh
adb shell settings put global stay_on_while_plugged_in 7
adb shell settings put system screen_off_timeout 1800000
```

Restore both afterwards (`0` and `120000`); they are the user's settings.

## 5. Driving the app

`adb shell uiautomator dump /sdcard/ui.xml` plus `adb shell cat /sdcard/ui.xml`
gives every node's text and bounds, which is enough to tap by coordinate and to
read state without screenshots. The voice screen prints its state as
`Voice: <state>`, so a shell loop can follow the whole state machine.

Text input needs `%s` for spaces: `adb shell input text 'two%swords'`.

Coordinates shift between screens and builds. Re-dump rather than reusing them.

## 6. Provider checks without a device

```sh
sh ops/credential_smoke.sh
```

Runs the credential-gated provider smokes against real Gemini in a verification
image. A **skip** means the API could not be reached, not that the check passed.
See `ops/README.md`.

To drive a real Live turn server-side with injected speech — useful for judging
the protocol without acoustics — synthesize 16 kHz PCM16 and feed it to the
adapter directly:

```sh
say -v Samantha -o q.aiff "What is the capital of Belarus?"
afconvert -f WAVE -d LEI16@16000 -c 1 q.aiff q.wav     # strip the 44-byte header
```

## 7. What the logs will and will not tell you

Backend logs are redacted by design: a provider failure surfaces as
`realtime_provider_unavailable` with no cause, and `_receive_loop` discards the
original exception ([OI-W6-003](OPEN_ISSUES.md)). Diagnosing a provider defect
currently means adding a temporary print and reverting it before committing.
Scrub `key=` from anything printed.

`websocket_completed` carries `close_code` only when the **server** closed. No
`close_code` means the client hung up — which is how [OI-W6-019](OPEN_ISSUES.md)
was eventually found.

The Android app logs nothing. Reading a client-side failure reason means adding
a temporary `android.util.Log` — note that `android.util.Log` throws in JVM unit
tests, so remove it before running them.

## 8. Cleaning up

```sh
docker compose -f docker-compose.yml -f ops/live.compose.yml stop backend
adb reverse --remove-all
adb shell settings put global stay_on_while_plugged_in 0
adb shell settings put system screen_off_timeout 120000
```
