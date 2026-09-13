# MIA open issues

Этот файл содержит только неблокирующие вопросы. Blocking conditions находятся в STATUS.md и handoff соответствующей session.

Статусы: OPEN, DECIDED, RESOLVED, SUPERSEDED, ESCALATED.

## Active index

Integration session обновляет этот индекс после merge.

| ID | Status | Component | Short question | Current assumption | Revisit by |
| --- | --- | --- | --- | --- | --- |
| OI-ARCH-001 | OPEN | Android identity | Какой финальный application ID? | временный уникальный ID до S21 | before S21 |
| OI-ARCH-002 | RESOLVED | Android platform | Какой minSdk? | API 26 для PoC | resolved in S20 |
| OI-ARCH-003 | OPEN | Wake word | Какая финальная активационная фраза? | tap-to-talk, затем временная phrase | before S27 |
| OI-ARCH-004 | OPEN | Tools | Какой внешний read-only tool идёт первым? | system.get_current_datetime, без внешнего API | S17 |
| OI-ARCH-005 | OPEN | Voice history | Насколько output transcript считается точным текстом ответа? | показывать как transcript с delivery metadata | S31 |
| OI-ARCH-006 | OPEN | Cost controls | Каковы точные daily/session Gemini limits? | консервативные config defaults | S18 |
| OI-S20-001 | RESOLVED | Android platform baseline | Какой minSdk использовать в scaffold? | API 26 принят | resolved in W1 |
| OI-S12-001 | OPEN | Android auth recovery | Как восстановиться после потерянного refresh response? | повторная аутентификация | S21/S30 |
| OI-S16-001 | OPEN | Gemini Live dependency | Закрепить WebSocket transport или обновить SDK? | текущий wire transport | S18 |

## Record template

~~~text
### OI-Sxx-NNN — Short title

- Status: OPEN
- Source session: Sxx
- Component:
- Question:
- Why non-blocking:
- Current assumption:
- Options:
- Recommendation:
- Affects sessions:
- Revisit trigger/by:
- Resolution:
- Evidence:
~~~

## Architecture inbox

### OI-ARCH-001 — Final Android application ID

- Status: OPEN
- Source session: architecture
- Component: Android identity
- Question: какой production application ID/package name использовать?
- Why non-blocking: S20 может применить явно помеченный временный уникальный ID.
- Current assumption: временный ID фиксируется S00/S20 и не публикуется в production.
- Options: домен владельца; нейтральный продуктовый домен.
- Recommendation: решить до настройки release OAuth client.
- Affects sessions: S20, S21, S28.
- Revisit trigger/by: before S21 real sign-in/release credentials.
- Resolution:
- Evidence:

### OI-ARCH-002 — Android minSdk

- Status: RESOLVED
- Source session: architecture
- Component: Android platform
- Question: какой minSdk соответствует реальным target devices?
- Why non-blocking: S20 может выбрать консервативный baseline и документировать его.
- Current assumption: API 26 (Android 8.0) для PoC; пересмотреть только при несовместимой зависимости или release device-matrix validation.
- Options: broader device coverage; newer API and simpler implementation.
- Recommendation: использовать API 26 baseline, зафиксированный S20.
- Affects sessions: S20–S28.
- Revisit trigger/by: S20.
- Resolution: S20 установила `minSdk = 26`, `targetSdk = 36` и `compileSdk = 36`.
- Evidence: Android `docs/adr/0001-android-min-sdk-26.md`, `gradle/libs.versions.toml`, S20 handoff и успешный W1 Gradle gate.

### OI-ARCH-003 — Final wake phrase

- Status: OPEN
- Source session: architecture
- Component: wake word
- Question: какая phrase является продуктовой и собирается для KWS evaluation?
- Why non-blocking: tap-to-talk и временная тестовая phrase позволяют завершить voice pipeline.
- Current assumption: phrase выбирается до S27.
- Options: «Привет, Мия»; другой брендовый вариант.
- Recommendation: короткая фонетически отличимая phrase, проверенная на RU/BY.
- Affects sessions: S27, S28.
- Revisit trigger/by: before S27.
- Resolution:
- Evidence:

### OI-ARCH-004 — First external tool

- Status: OPEN
- Source session: architecture
- Component: tools
- Question: нужен ли weather provider уже в PoC?
- Why non-blocking: system.get_current_datetime доказывает function calling без внешней зависимости.
- Current assumption: S17 начинает с system.get_current_datetime.
- Options: system time; weather with an adapter.
- Recommendation: time first, weather as follow-up.
- Affects sessions: S17, S32.
- Revisit trigger/by: S17.
- Resolution:
- Evidence:

### OI-ARCH-005 — Output transcript semantics

- Status: OPEN
- Source session: architecture
- Component: conversation/voice
- Question: считать ли Gemini output transcription точным каноническим текстом произнесённого audio?
- Why non-blocking: можно хранить его как transcript с provider/delivery metadata.
- Current assumption: не обещать побуквенную точность.
- Options: transcript only; separate authored text if provider supplies it.
- Recommendation: измерить в S31.
- Affects sessions: S16, S22, S23, S31.
- Revisit trigger/by: S31.
- Resolution:
- Evidence:

### OI-ARCH-006 — Gemini quotas

- Status: OPEN
- Source session: architecture
- Component: cost controls
- Question: какие session duration/daily audio/token limits достаточны для internal beta?
- Why non-blocking: config может начать с консервативных limits.
- Current assumption: one live session/user, short idle timeout, daily cap.
- Options: fixed limits; environment-specific limits.
- Recommendation: environment-specific values, measured in S31.
- Affects sessions: S16, S18, S31.
- Revisit trigger/by: S18/S31.
- Resolution:
- Evidence:

## S00 inbox

S00 completed with no new session-specific open issues. The provisional Android
namespace remains tracked by OI-ARCH-001 and ADR-008.

## S10 inbox

S10 completed with no new session-specific open issues. Local runtime defaults
to the fake provider; real provider behaviour remains owned by later sessions.

## S20 inbox

<!-- S20 adds OI-S20-* records here. -->

### OI-S20-001 — OI-ARCH-002 minSdk decision update

- Status: RESOLVED
- Source session: S20
- Component: Android platform baseline
- Question: what minSdk should the Android PoC scaffold use?
- Current assumption: API 26 (Android 8.0).
- Options: lower floor for broader coverage; API 26 baseline; newer floor.
- Recommendation: retain API 26 unless a later Credential Manager,
  assistant-role, or audio dependency proves it incompatible.
- Affected future sessions: S21–S28.
- Revisit trigger/by: before adding a dependency that requires API 27+ or before
  release device-matrix validation.
- Resolution: S20 set `minSdk = 26` and `targetSdk = 36`; the rationale is
  recorded in Android ADR-001. The W1 integrator consolidated the decision into
  OI-ARCH-002.
- Evidence: `mia-helper-android/docs/adr/0001-android-min-sdk-26.md`,
  `gradle/libs.versions.toml`, and S20 Gradle verification.

## S11 inbox

S11 completed with no new session-specific open issues. The remaining schema
areas belong to explicitly scheduled downstream sessions and are not hidden
blockers for conversation persistence.

## S13 inbox

S13 completed with no new session-specific open issues. Real Gemini smoke is a
credential-dependent verification step recorded in the handoff, not a product
decision or a blocker for the deterministic provider boundary.

## S15 inbox

S15 completed with no new session-specific open issues. Production auth/provider
composition and durable reconnect state remain explicitly owned by downstream
sessions rather than hidden as protocol decisions.

## S24 inbox

<!-- S24 adds OI-S24-* records here. -->

No new non-blocking questions. The mandatory physical-device verification is
complete and recorded in the S24 handoff.

## S12 inbox

<!-- S12 adds OI-S12-* records here. -->

### OI-S12-001 — Auth client retry policy

- Status: OPEN
- Source session: S12
- Component: Android authentication/session recovery
- Question: how should the client recover when a refresh response is lost?
- Why non-blocking: backend nonce/reuse protections have a secure defined policy.
- Current assumption: serialize refresh per login, atomically replace tokens,
  discard the local session and reauthenticate after an ambiguous refresh outcome.
- Options: explicit reauthentication; future reviewed protocol with secure retry
  receipts and a bounded retry window.
- Recommendation: use explicit reauthentication for the PoC. Do not retry an old
  refresh token or let Idempotency-Key bypass reuse detection. Repeated nonce
  creation returns a fresh nonce; token-bearing responses are never cached.
- Affected future sessions: S21, S30, S18.
- Revisit trigger/by: S21 session repository and S30 lost-response integration tests.
- Resolution: backend behavior documented; Android implementation remains downstream.
- Evidence: `app/modules/auth/README.md`, `tests/auth/test_service_postgres.py`.

## S14 inbox

<!-- S14 adds OI-S14-* records here. -->

S14 completed with no new non-blocking product questions. Runtime mounting and
S12 bearer composition are assigned to the W3 integrator in the S14 handoff;
the session's REST/SSE lifecycle, ownership, retry, and disconnect gates pass.

## S16 inbox

### OI-S16-001 — Own the Live transport dependency during hardening

- Status: OPEN
- Source session: S16
- Component: backend/Gemini Live dependency
- Question: should S18 declare the already locked `websockets==15.0.1` directly,
  or upgrade `google-genai==1.10.0` and migrate Live transport to its public API?
- Why non-blocking: the official WebSocket transport supports input transcription,
  audio-stream-end and setup acknowledgement with the existing locked dependencies.
- Current assumption: retain the isolated wire transport and SDK message validation.
  The pinned SDK's MLDev connector rejects input transcription; its transparent
  resumption flag is also unsupported. No SDK private member is patched or accessed.
- Options: declare `websockets==15.0.1` directly; upgrade SDK and replace transport.
- Recommendation: the shared dependency owner should add `websockets==15.0.1` to
  project dependencies and regenerate the lockfile in S18; any SDK migration needs
  the same fixture, loopback and credential-configured smoke checks.
- Affects sessions: S18, S31.
- Revisit trigger/by: S18 dependency and deployment hardening.
- Resolution:
- Evidence: `app/providers/ai/gemini_live/README.md`,
  `tests/providers/test_gemini_live_transport.py`, and the existing `uv.lock` pin.

## S25 inbox

<!-- S25 adds OI-S25-* records here. -->

No new non-blocking questions. The physical-device gate is complete and recorded
in the S25 handoff.

## S17 inbox

<!-- S17 adds OI-S17-* records here. -->

No new non-blocking questions. Decision update for OI-ARCH-004: S17 implemented
`system.get_current_datetime` as the first allow-listed read-only tool; weather
is deferred. The W4 integrator should consolidate this into the architecture
record after the runtime composition gate.

## S21 inbox

No new non-blocking questions. OI-ARCH-001 remains the Android identity decision;
OI-S12-001's explicit reauthentication policy is implemented in S21. The
unmet real-device sign-in gate is a blocker recorded in the S21 handoff, not an
OPEN_ISSUES entry.

## S22 inbox

### OI-S22-001 — Incremental entry sync cursor

- Status: OPEN
- Source session: S22
- Component: Android history / conversation REST
- Question: should REST expose an opaque cursor after the final entry, or an additive `after_sequence` query, so reconnect can fetch only entries newer than the last cached server sequence?
- Why non-blocking: S22 safely traverses all opaque keyset pages from the beginning and merges by server ID and sequence; PoC history remains correct after restart.
- Current assumption: keep REST cursors opaque and perform full keyset traversal on reconnect; persist `lastSequence` for reconciliation and future incremental sync.
- Options: continue full traversal; add final-page sync cursor; add `after_sequence` query.
- Recommendation: add a server-issued final-page sync cursor before large histories or beta scale, without client-side cursor construction.
- Affects sessions: S18, S23, S30.
- Revisit trigger/by: S18 hardening or measured large-history sync cost.
- Resolution:
- Evidence: S14 REST emits `next_cursor` only when another page exists; S22 restart test covers full traversal and ID/sequence merge.

## S26 inbox

<!-- S26 adds OI-S26-* records here. -->

## S18 inbox

<!-- S18 adds OI-S18-* records here. -->

## S23 inbox

<!-- S23 adds OI-S23-* records here. -->

## S27 inbox

<!-- S27 adds OI-S27-* records here. -->

## S28 inbox

<!-- S28 adds OI-S28-* records here. -->

## S30 inbox

<!-- S30 adds OI-S30-* records here. -->

## S31 inbox

<!-- S31 adds OI-S31-* records here. -->

## S32 inbox

<!-- S32 adds OI-S32-* records here. -->

## S33 inbox

<!-- S33 adds OI-S33-* records here. -->
