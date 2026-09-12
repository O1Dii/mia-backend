# Gemini Live adapter

`GeminiLiveAdapterFactory(config, resume_store=...)` implements the existing
`RealtimeProviderFactory` consumed by `RealtimeGateway`. Configuration is explicit;
no environment or secret-value file is loaded. Runtime routing/auth composition
remains with the API/hardening owner. No public realtime schema was changed.

`GeminiLiveConfig` takes the documented `GEMINI_API_KEY`, `GEMINI_LIVE_MODEL` and
`GEMINI_REQUEST_TIMEOUT_SECONDS` values through the composing application. The key
must be a `SecretStr`; the actual model ID is private configuration, while usage
events expose only `model_alias`. Server instructions include the negotiated locale.
Transcript `language` currently carries this locale as a hint, not detected language.

## Provider connection and limits

The transport sends the official v1beta WebSocket setup and requires
`setupComplete` before configure returns. It uses the already locked WebSockets
dependency; the shared `google-genai==1.10.0` connector rejects input transcription
on the Developer API, so the adapter uses Google message types for validation only.
The SDK's unsupported `transparent` flag is never sent. OI-S16-001 records ownership
of the dependency declaration/possible later SDK migration.

Setup enables AUDIO output, server VAD, interruption by user activity, negotiated
input/output transcription, token limits, context compression, and session
resumption. It sends declared function schemas only; no callable is passed or run.

Input uses base64 PCM16 little-endian mono 16 kHz on the provider wire. Public MIA
frames remain binary bytes. Commit sends `realtimeInput.audioStreamEnd`. Output is
validated as complete PCM16 samples at 24 kHz and split into at most 4096-byte
frames before passing to the gateway. Set `output_frame_bytes` to the gateway's
frame limit if overriding its default. The gateway owns public sequence numbers
and response-ID filtering.

The provider WebSocket has a 1 MiB message limit and a 16-frame receive queue;
the adapter output queue defaults to 32 events. At most 32 pre-commit messages,
8 pending turns, 32 pending function calls and 65,536 transcript characters per
direction/turn are retained. Overflow fails the session instead of silently
dropping input. Sends, connection/setup, queue writes and resume-store calls have
request timeouts. A separate receive idle timeout defaults to 90 seconds.

WebSocket logging uses a disabled, non-propagating logger, including under DEBUG:
the provider URL contains the key and frame payloads contain conversation data.
Failures crossing the adapter boundary have fixed messages and suppressed raw
exception chains. Close is idempotent, clears buffered output and ephemeral state,
and bounds cleanup. Raw audio is never written to disk.

## Turn identity, transcripts and cancellation

The current provider port supplies stream/turn IDs only at `commit_audio`, so
early provider messages are buffered within the stated bound. A second input's
transcription is deferred separately while the preceding response is interrupted.
Speech start/stop events are MIA commit boundaries with offsets derived from byte
counts; they are not precise server VAD timestamps. S26 should keep the local VAD
commit timely and measure this behaviour with the chosen model.

Transcript fragments append in order, including repeated identical fragments.
Provider `finished` flags are accepted where available; otherwise generation/turn
completion finalizes accumulated transcripts. All model parts and an accompanying
transcript are processed; thought parts are discarded. Input transcription has no
provider ordering guarantee: a late complete transcript can attach to the previous
turn if new input has not begun; appending to an already finalized transcript fails
safely. OI-ARCH-005 remains the output-transcript fidelity decision for S31.

`response.cancel` immediately suppresses further local output for that response.
With automatic server VAD, the next real microphone activity interrupts provider
generation; the adapter does not inject artificial speech or incompatible manual
activity events. A cancel without new speech can therefore continue provider
compute until Gemini finishes. Provider `interrupted` maps to
`response.interrupted`; the following `turnComplete` completes it as interrupted.

Provider function IDs become deterministic MIA UUIDs. Declarations supply trusted
confirmation metadata; provider arguments remain untrusted. Cancellation emits
`tool.call.completed` with `status=cancelled`. The existing port has no tool-result
method: S17 owns authorized execution and the result path; default tools are empty.

## Resumption

Resumption handles never appear in public events or object representations.
`GeminiLiveResumeStore` provides load/save/delete operations; the default is an
active-session in-memory store. S18 can bind Redis with an expiry and deployment
ownership rules. Close deletes the handle. New audio invalidates an old checkpoint;
only a subsequent resumable provider update can establish a new one.

GoAway/clean end reconnects with the latest valid handle only between turns when
no input or deferred messages remain. Retries are bounded. Active/uncertain input,
non-resumable state, transport failure or idle expiry terminates with a sanitized
provider error; the gateway marks the session interrupted. There is no unacknowledged
PCM replay, client reconnect guarantee, or recovery of a partially delivered turn.
Canonical history restoration and durable stores remain downstream composition.
GoAway during an active turn currently ends the session immediately; S31 must
measure whether waiting for a safe boundary before its deadline is needed.

## Verification

Run the repository's Python environment from this checkout:

```sh
python -m pytest tests/providers/test_gemini_live_mapping.py tests/providers/test_gemini_live_adapter.py tests/providers/test_gemini_live_transport.py -q
python -m pytest tests/providers/test_gemini_live_smoke.py -q -ra
```

The first suite is deterministic and includes a real loopback WebSocket codec test
with synthetic JSON/audio fixtures. The second is a connection/PCM-send smoke,
enabled only when both `GEMINI_API_KEY` and `GEMINI_LIVE_MODEL` exist in process
environment. It does not load `.env` and is skipped without those variables.
This smoke does not establish speech recognition/voice quality; S31 owns voice E2E
with the selected model, latency, transcription and provider quota measurements.

## Official references

Protocol and capabilities checked on 2026-09-11/12:

- [WebSocket connection and authentication](https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket)
- [Wire fields, setup acknowledgement and interruption](https://ai.google.dev/api/live)
- [PCM formats, transcription and automatic VAD](https://ai.google.dev/gemini-api/docs/live-api/capabilities)
- [Session resumption and context compression](https://ai.google.dev/gemini-api/docs/live-api/session-management)
- [Manual function-call lifecycle](https://ai.google.dev/gemini-api/docs/live-api/tools)
