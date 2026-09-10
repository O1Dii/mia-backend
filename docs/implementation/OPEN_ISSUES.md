# MIA open issues

Этот файл содержит только неблокирующие вопросы. Blocking conditions находятся в STATUS.md и handoff соответствующей session.

Статусы: OPEN, DECIDED, RESOLVED, SUPERSEDED, ESCALATED.

## Active index

Integration session обновляет этот индекс после merge.

| ID | Status | Component | Short question | Current assumption | Revisit by |
| --- | --- | --- | --- | --- | --- |
| OI-ARCH-001 | OPEN | Android identity | Какой финальный application ID? | временный уникальный ID до S21 | before S21 |
| OI-ARCH-002 | OPEN | Android platform | Какой minSdk? | выбрать в S20 по нужным APIs/device targets | S20 |
| OI-ARCH-003 | OPEN | Wake word | Какая финальная активационная фраза? | tap-to-talk, затем временная phrase | before S27 |
| OI-ARCH-004 | OPEN | Tools | Какой внешний read-only tool идёт первым? | system.get_current_datetime, без внешнего API | S17 |
| OI-ARCH-005 | OPEN | Voice history | Насколько output transcript считается точным текстом ответа? | показывать как transcript с delivery metadata | S31 |
| OI-ARCH-006 | OPEN | Cost controls | Каковы точные daily/session Gemini limits? | консервативные config defaults | S18 |

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

- Status: OPEN
- Source session: architecture
- Component: Android platform
- Question: какой minSdk соответствует реальным target devices?
- Why non-blocking: S20 может выбрать консервативный baseline и документировать его.
- Current assumption: выбрать минимальный API после проверки Credential Manager/audio/assistant requirements.
- Options: broader device coverage; newer API and simpler implementation.
- Recommendation: S20 фиксирует решение ADR.
- Affects sessions: S20–S28.
- Revisit trigger/by: S20.
- Resolution:
- Evidence:

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

<!-- S10 adds OI-S10-* records here. -->

## S20 inbox

<!-- S20 adds OI-S20-* records here. -->

### OI-S20-001 — OI-ARCH-002 minSdk decision update

- Status: DECIDED
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
  recorded in Android ADR-001. The source OI-ARCH-002 record remains for the
  wave integrator to consolidate under its ownership.
- Evidence: `mia-helper-android/docs/adr/0001-android-min-sdk-26.md`,
  `gradle/libs.versions.toml`, and S20 Gradle verification.

## S11 inbox

<!-- S11 adds OI-S11-* records here. -->

## S13 inbox

<!-- S13 adds OI-S13-* records here. -->

## S15 inbox

<!-- S15 adds OI-S15-* records here. -->

## S24 inbox

<!-- S24 adds OI-S24-* records here. -->

## S12 inbox

<!-- S12 adds OI-S12-* records here. -->

## S14 inbox

<!-- S14 adds OI-S14-* records here. -->

## S16 inbox

<!-- S16 adds OI-S16-* records here. -->

## S25 inbox

<!-- S25 adds OI-S25-* records here. -->

## S17 inbox

<!-- S17 adds OI-S17-* records here. -->

## S21 inbox

<!-- S21 adds OI-S21-* records here. -->

## S22 inbox

<!-- S22 adds OI-S22-* records here. -->

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
