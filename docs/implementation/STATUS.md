# MIA implementation status

Обновляется wave integrator после проверки handoff и commits. Worker sessions не объявляют downstream session READY самостоятельно.

Допустимые состояния: PENDING, READY, IN_PROGRESS, COMPLETED, PARTIAL, BLOCKED, FAILED, SUPERSEDED.

| ID | Wave | Status | Dependencies | Preferred executor | Handoff | Integrated commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S00 | W0 | COMPLETED | — | Fable 5.1 high | [S00](handoffs/S00.md) | b56c17f | contracts/governance baseline verified |
| S10 | W1 | COMPLETED | S00 | Terra high | [S10](handoffs/S10.md) | c7a2846 | backend scaffold verified |
| S20 | W1 | COMPLETED | S00 | Terra high | [S20](handoffs/S20.md) | 065617a | Android scaffold verified; code at 21a31ea |
| S11 | W2 | COMPLETED | S10 | Sol high | [S11](handoffs/S11.md) | 79e1d1d | PostgreSQL persistence verified |
| S13 | W2 | COMPLETED | S00,S10 | Sol xhigh | [S13](handoffs/S13.md) | dd73636 | text provider/orchestration verified |
| S15 | W2 | COMPLETED | S00,S10 | Sol xhigh | [S15](handoffs/S15.md) | 52f1a74 | realtime fake/gateway verified |
| S24 | W2 | COMPLETED | S20 | Sol xhigh | [S24](handoffs/S24.md) | 3b7e8eb | Android main at 9a4e7f3; physical device gate verified |
| S12 | W3 | COMPLETED | S10,S11 | Sol high | [S12](handoffs/S12.md) | c86b0ae | auth mounted; Google dependency pinned |
| S14 | W3 | COMPLETED | S11,S13 | Sol high | [S14](handoffs/S14.md) | c86b0ae | REST/SSE mounted and verified |
| S16 | W3 | COMPLETED | S13,S15 | Fable 5.1 xhigh | [S16](handoffs/S16.md) | c86b0ae | Gemini Live factory integrated; cloud smoke remains S31 |
| S25 | W3 | COMPLETED | S15,S20,S24 | Sol xhigh | [S25](handoffs/S25.md) | c86b0ae | Android main at 6e7395b; physical gate verified |
| S17 | W4 | COMPLETED | S13,S14,S15 | Fable 5.1 xhigh | [S17](handoffs/S17.md) | e9ca3f1 | read-only tool policy/audit integrated; backend gate PASS |
| S21 | W4 | COMPLETED | S12,S20 | Sol high | [S21-R2](handoffs/S21-R2.md) | b7a6c10 | real sign-in/refresh/logout/re-login PASS on SM-S938B |
| S22 | W4 | COMPLETED | S11,S14,S20 | Sol high | [S22](handoffs/S22.md) | e9ca3f1 | Android main 452949c; cache/Room gate PASS |
| S26 | W4 | COMPLETED | S16,S24,S25 | Fable 5.1 xhigh | [S26-R1](handoffs/S26-R1.md) | e9ca3f1 | Android main 7db3e6f; physical flush/cancel 83/84 ms PASS |
| S18 | W5 | COMPLETED | S12,S14,S16,S17 | Sol high | [S18](handoffs/S18.md) | adccdfc | hardening/deploy gate re-verified by integrator |
| S23 | W5 | COMPLETED | S14,S20,S22 | Terra high | [S23](handoffs/S23.md) | adccdfc | Android main 3b6f7b1; chat wired into navigation |
| S27 | W5 | PARTIAL | S24,S26 | Sol xhigh | [S27](handoffs/S27.md) | adccdfc | DEFERRED out of PoC scope (ADR-009); merged, disabled by default |
| S28 | W6 | PENDING | S27 | Sol xhigh | — | — | DEFERRED out of PoC scope (ADR-009); not started |
| S30 | W6 | COMPLETED | S12,S14,S21,S23 | Sol high | [S30](handoffs/S30.md) | d8db5ce | real sign-in → Gemini text → restart → retry PASS on SM-S938B |
| S31 | W6 | PARTIAL | S16,S25,S26 | Fable 5.1 xhigh | [S31](handoffs/S31.md) | d8db5ce / Android ed8e05f | voice made reachable; one real round trip; soak/barge-in/latency NOT measured |
| S32 | W6 | PARTIAL | S17,S23,S25 | Sol high | [S32](handoffs/S32.md) | 5b05d0f | text modality closed on device against real Gemini; voice modality waits on S31 |
| S33 | W7 | PENDING | S18,S30,S31,S32 | Fable 5.1 xhigh | — | — | release gate; inherits two PARTIAL inputs |
| S17-R1 | W6 | COMPLETED | S17 | Opus 5 high | [S17-R1](handoffs/S17-R1.md) | d8db5ce | Gemini tool schema translation |
| S16-R1 | W6 | COMPLETED | S16 | Opus 5 high | [S16-R1](handoffs/S16-R1.md) | d8db5ce | Live input transcript continuation |
| S25-R1 | W6 | COMPLETED | S25 | Opus 5 high | [S25-R1](handoffs/S25-R1.md) | Android ed8e05f | playback backpressure |
| S13-R1 | W6 | COMPLETED | S13 | Opus 5 high | [S13-R1](handoffs/S13-R1.md) | 2edb942 | Gemini 3.x rejects the tool continuation; text pinned to 2.5 |
| S15-R1 | W6 | COMPLETED | S15 | Opus 5 high | [S15-R1](handoffs/S15-R1.md) | 5b05d0f | silent and noisy Live sessions no longer die |
| S16-R2 | W6 | COMPLETED | S16 | Opus 5 high | [S16-R2](handoffs/S16-R2.md) | 485147b | one VAD: manual activity detection driven by the client's commit |
| S18-R1 | W6 | COMPLETED | S18 | Opus 5 high | [S18-R1](handoffs/S18-R1.md) | 3516dcd | verification image; credential smokes execute for the first time since W3 |
| S25-R2 | W6 | COMPLETED | S25 | Opus 5 high | [S25-R2](handoffs/S25-R2.md) | Android f055769 | a server-initiated close now reaches the client; confirmed on device |
| S25-R3 | W6 | COMPLETED | S25 | Opus 5 high | [S25-R3](handoffs/S25-R3.md) | Android 30a4ea8 | playback backpressure off the event stream; three spoken turns hold on device |

## Current wave gate

W6 is integrated and **PARTIAL**. Both repositories are on `main`: backend
`37922dd`, Android `30a4ea8`. No session branch holds unmerged work.

The wave's main result is that running the product against real Gemini for the
first time found four defects that every green suite had missed, and all four
are fixed with regressions that fail on the pre-fix code:

1. [S17-R1](handoffs/S17-R1.md) — the registry's raw JSON Schema was rejected by
   both Gemini surfaces, so **every** real text turn and **every** Live session
   failed. Provider fixtures used `{"type": "object"}` and never saw it.
2. [S16-R1](handoffs/S16-R1.md) — the Live mapper treated a transcript fragment
   after a final one as a protocol violation, so a session died the moment a user
   spoke a second time.
3. [S25-R1](handoffs/S25-R1.md) — a full playback jitter buffer failed the turn
   instead of applying backpressure, killing every cloud answer ~26 ms in.
4. [S32](handoffs/S32.md) — a cancelled tool call's audit row stayed `running`
   forever, so S17's "cancellation is recorded" guarantee was false.

S31 also found that **voice was not reachable in the app at all**: the voice
destination rendered `Text("Voice scaffold")`, so S25 and S26 had never run
inside the product. It is now wired.

Gates at the integrated head: ruff/format, mypy (108 files), 180 passed /
1 credential skip against PostgreSQL 16 and Redis 7, `alembic check`, contract
validation (5 schemas, 18 operations, 36 fixtures, 25 error codes); Android
`verifyContractFixtures test lint assembleDebug` with 80 unit tests and both
debug APKs.

S30 is COMPLETE on a physical SM-S938B against real Gemini: clean install,
Google sign-in, conversation, streamed answer, process restart, persistent
history, refresh, and retry of a failed turn reusing its durable operation.

S31 and S32 are PARTIAL, and the gap is measurement, not implementation:

- S31 observed one complete round trip — device microphone → backend → Gemini
  Live → response transcripts and audio frames back on the device. **Audible
  playback, the 20-turn soak, interruption against cloud audio, reconnect and
  every latency figure are unmeasured.** The blocker is acoustic: the phone
  captured synthesized speech at ~8 % of full scale, which its own VAD detects
  but Gemini does not reliably treat as speech. Closing it needs the phone close
  to the sound source or a person speaking to it.
- S32's **text modality is now closed on the device** against real Gemini: the
  model chose `system.get_current_datetime`, the executor ran it under policy,
  the answer came back as "The current date and time in Europe/Minsk is
  2026-09-20 01:31:17.", and the audit row and canonical `tool_call`/`tool_result`
  entries are in place. The voice modality still waits on S31.

A second, acoustic-free round after the wave closed added three more repairs:

5. [S13-R1](handoffs/S13-R1.md) — the whole Gemini 3.x family rejects the tool
   continuation turn, because MIA rebuilds the `functionCall` part without the
   `thought_signature` the model requires. The tool ran and was audited
   `succeeded`, then the turn failed. Text is pinned to `gemini-2.5-flash`.
6. [S15-R1](handoffs/S15-R1.md) — three ways a healthy Live session died for
   being quiet: an inbound-only idle timeout reported as "provider unavailable",
   a vanished client holding the only session slot, and ordinary room noise
   overflowing the pre-commit buffer in ~30 s. A session now holds 180 s on the
   device without failing, and a killed app's slot is reusable at once.

`GEMINI_TEXT_MODEL` must be a 2.5 text model and `GEMINI_LIVE_MODEL` must be
`gemini-2.5-flash-native-audio-latest` ([OI-W6-004](OPEN_ISSUES.md),
[OI-W6-013](OPEN_ISSUES.md)). Newer models are not drop-in replacements:
`gemini-3.8-live` connects and never answers, `gemini-3.1-flash-live-preview`
does not connect, and every 3.x text model breaks tools.

7. [S16-R2](handoffs/S16-R2.md) — the deepest finding of the wave, and now
   closed: MIA ran **two voice activity detectors against each other**. The
   contract is client-commit-driven, but the Live setup left Gemini's automatic
   detection on, so the provider opened turns the client never committed.
   Automatic detection is disabled and the turn is now bounded by explicit
   `activityStart`/`activityEnd`. Against real Gemini the same speech produces a
   full answer when MIA commits and **zero events** when it does not — the
   property the contract always assumed and never had. A device session held
   180 s in `listening` without a spurious turn.

8. [S18-R1](handoffs/S18-R1.md) — the credential-gated provider smokes had
   nowhere to execute and had been reporting SKIP since W3, which reads as
   coverage. That is how the three cloud-only defects above reached a device
   session unnoticed. `Dockerfile.test` and `ops/credential_smoke.sh` give them
   a home; the Live smoke passed against the real API for the first time, and
   the release image is untouched.

9. [S25-R2](handoffs/S25-R2.md) — a close the server started reached the client
   never: OkHttp delivers `onClosed` only once the peer's close frame is
   answered, and the listener never answered. The gateway hung up and the voice
   screen waited forever. The controller was blameless; it was simply never
   given the event. Confirmed on the device: stopping the backend mid-session
   now surfaces a retryable error within 3 s.

10. [S25-R3](handoffs/S25-R3.md) — and the defect S25-R2 made visible turned
    out to be **this wave's own** [S25-R1](handoffs/S25-R1.md). Waiting for
    playback room inside the inbound event collector stalled the client's
    32-slot queue, so the client closed its own socket with 1013 on the first
    real answer. The failed barge-in everyone could see was two steps
    downstream. A response's audio now waits in a queue of its own.

With that, **three consecutive spoken turns completed on the device against real
Gemini without the session ending** — each `listening → waiting → listening`,
with the WebSocket never closing. That is the first working voice loop observed
in this wave. Audibility is still unconfirmed: nobody listened to the phone.

No barge-in latency figure currently stands. S26-R1's 83/84 ms were measured on
the previous protocol against a fake gateway, and barge-in now also reaches the
provider through `activityStart` ([OI-W6-017](OPEN_ISSUES.md)). S31-R1 must
re-measure before anyone quotes a number.

## Blocking conditions

The procedure for running any of this on a phone is in
[DEVICE_GATE.md](DEVICE_GATE.md); it is no longer something a session has to
reconstruct.

S33 must not be started as a clean release gate: two of its four inputs are
PARTIAL. What remains is now measurement rather than repair. One bounded session
(S31-R1) with the phone at the sound source, or a person speaking to it, closes
the voice measurements and the voice tool smoke: the 20-turn soak can now be
attempted, because sessions survive real answers. No new authority is needed and
no code change is known to be outstanding.

Wake-phrase activation remains out of internal-beta scope
([ADR-009](../adr/0009-defer-wake-word-activation.md)); S27 and S28 are
unchanged by this wave.

## Integration history

| Wave | Integrated at | Commit | Gate result | Integrator |
| --- | --- | --- | --- | --- |
| W0 | 2026-09-10 | b56c17f | PASS | S00 bootstrap |
| W1 | 2026-09-10 | 065617a | PASS | parallel S10/S20 + wave integration |
| W2 | 2026-09-11 | 3b7e8eb | PASS | parallel W2 + S24 physical-device closeout |
| W3 | 2026-09-12 | c86b0ae | PASS | parallel W3 + cross-repository wave integration |
| W4 | 2026-09-14 | e9ca3f1 / Android 7db3e6f | PARTIAL | parallel W4 + S26 physical closeout; S21 OAuth gate pending |
| W5 | 2026-09-16 | adccdfc / Android 3b6f7b1 | PASS | parallel S18 + Android S23/S27 integration; S21-R2 auth closeout at b7a6c10; wake deferred by ADR-009 |
| W6 | 2026-09-20 | 37922dd / Android 30a4ea8 | PARTIAL | parallel S30/S32 + S31 device work; nine defects repaired plus the verification gap that hid them (S18-R1); three spoken turns hold on device; acoustic measurements outstanding |
