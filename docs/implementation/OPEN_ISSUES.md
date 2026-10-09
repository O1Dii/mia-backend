# MIA open issues

Этот файл содержит только неблокирующие вопросы. Blocking conditions находятся в STATUS.md и handoff соответствующей session.

Статусы: OPEN, DECIDED, RESOLVED, SUPERSEDED, ESCALATED.

## Active index

Integration session обновляет этот индекс после merge.

| ID | Status | Component | Short question | Current assumption | Revisit by |
| --- | --- | --- | --- | --- | --- |
| OI-ARCH-001 | OPEN | Android identity | Какой финальный application ID? | временный ID для PoC, не release OAuth | before S21-R1/release OAuth |
| OI-ARCH-002 | RESOLVED | Android platform | Какой minSdk? | API 26 для PoC | resolved in S20 |
| OI-ARCH-003 | SUPERSEDED | Wake word | Какая финальная активационная фраза? | activation отложена (ADR-009) | если activation вернётся в scope |
| OI-ARCH-004 | RESOLVED | Tools | Какой внешний read-only tool идёт первым? | system.get_current_datetime реализован | resolved in W4 |
| OI-ARCH-005 | OPEN | Voice history | Насколько output transcript считается точным текстом ответа? | показывать как transcript с delivery metadata | S31 |
| OI-ARCH-006 | DECIDED | Cost controls | Каковы точные daily/session Gemini limits? | request-count admission defaults в config, см. S18 inbox | S31 измеряет реальный расход |
| OI-S20-001 | RESOLVED | Android platform baseline | Какой minSdk использовать в scaffold? | API 26 принят | resolved in W1 |
| OI-S12-001 | DECIDED | Android auth recovery | Как восстановиться после потерянного refresh response? | повторная аутентификация реализована | S30 E2E |
| OI-S16-001 | RESOLVED | Gemini Live dependency | Закрепить WebSocket transport или обновить SDK? | `websockets==15.0.1` объявлен прямой зависимостью | resolved in S18 |
| OI-S22-001 | OPEN | Android history | Нужен ли incremental sync cursor? | полный keyset traversal | S30 с измеренной стоимостью history |
| OI-S18-001 | OPEN | Backend auth secrets | Как ротировать JWT signing key без инвалидации refresh hashes? | ротация выводит все сессии | before beta key rotation |
| OI-S18-002 | OPEN | Backend runtime | Realtime claims/limits/circuits/metrics живут в процессе | ровно один backend instance | before горизонтального масштабирования |
| OI-S18-003 | OPEN | Cost controls | Нужны ли Live idle timeout и token/audio budgets? | request-count proxies из OI-ARCH-006 | S31 |
| OI-W5-001 | OPEN | Wake word privacy | Должен ли logout удалять локальные wake-шаблоны голоса? | logout выключает wake, шаблоны остаются на устройстве | before shared-device beta |
| OI-W5-002 | OPEN | Android chat | Как отбросить pending-операцию для удалённого на сервере conversation? | операция остаётся retryable без discard-действия | S30 |
| OI-W6-001 | OPEN | Provider tests | Почему ни один тест не подавал провайдеру реальную декларацию из registry? | фикстуры используют `{"type": "object"}` | S33 |
| OI-W6-002 | OPEN | Live transcripts | Какому turn_id принадлежит реплика, расшифрованная до commit? | приписывается предыдущему turn | S33 |
| OI-W6-003 | OPEN | Live diagnostics | Как отличить дефект mapping от сетевого сбоя в логах? | никак: причина поглощается | before beta |
| OI-W6-004 | DECIDED | Gemini Live model | Какая Live-модель поддерживается адаптером? | `gemini-2.5-flash-native-audio-latest` | S31-R1 |
| OI-W6-005 | OPEN | Barge-in | Сохраняются ли 83/84 ms после изменения playback-пути? | не измерено заново | S31-R1 |
| OI-W6-006 | OPEN | Backend composition | Нужен ли явный override verifier/provider в `install_runtime_api`? | monkeypatch атрибутов модуля | S33 |
| OI-W6-007 | OPEN | Auth config | Нужен ли более низкий пол `auth_access_token_ttl_seconds` для тестов? | токен выпускается вручную | S33 |
| OI-W6-008 | RESOLVED | Live idle | Должна ли тишина 90 s завершать Live-сессию как «провайдер недоступен»? | нет; простой считается по обоим направлениям | resolved in S15-R1 |
| OI-W6-009 | RESOLVED | Live session slots | Как освободить слот после убийства приложения? | гейтвей закрывает мёртвый сокет | resolved in S15-R1 |
| OI-W6-010 | OPEN | Realtime gateway | Стоит ли гейтвею повторно отменять tool-задачи при закрытии? | отменяет; executor это терпит | S33 |
| OI-W6-011 | OPEN | Tool cancellation | Должен ли клиент узнавать об отмене tool-вызова? | видит только `response.interrupted` | S33 |
| OI-W6-012 | OPEN | Tool audit | Почему у отменённой записи `error_code = tool_execution_failed`? | статус и код расходятся | S33 |
| OI-W6-013 | RESOLVED | Tool contract | Нужен ли непрозрачный provider-токен в ToolCallRequested/ToolFeedback? | `provider_state` в портах M00, адаптеры M11 | resolved in MW1 integration |
| OI-W6-014 | RESOLVED | Credential smokes | Где запускать smoke-тесты, требующие ключа? | в `Dockerfile.test` через `ops/credential_smoke.sh` | resolved in S18-R1 |
| OI-W6-015 | OPEN | Realtime runtime | Должно ли окно неактивности клиента настраиваться оператором? | константа 30 s в модуле | S33 |
| OI-W6-016 | RESOLVED | Android voice | Клиент не замечает закрытый сокет в состоянии ожидания ответа | OkHttp не отдавал onClosed без ответа на close-фрейм | resolved in S25-R2 |
| OI-W6-017 | OPEN | Barge-in | Два механизма перебивания сразу: локальный flush и provider-side interrupt | не измерены вместе | S31-R1 |
| OI-W6-018 | OPEN | CI | Кто и когда запускает credential smoke? | документированный ручной шаг | S33 |
| OI-W6-019 | RESOLVED | Android voice | Голосовая сессия иногда обрывается сама через 7–45 с | клиент сам закрывал сокет кодом 1013 | resolved in S25-R3 |
| OI-M00-001 | RESOLVED | MVP contracts | Где реестр кодов ошибок mvp/v1? | `contracts/mvp/v1/error-codes.json` (M12) | resolved in MW1 integration |
| OI-M00-002 | OPEN | Memory contract | Категории памяти и `Fact.scope` окончательные? | предварительные | M17 |
| OI-M00-003 | RESOLVED | Backend tooling | Какую версию Python закрепить для `uv sync`? | `.python-version` = 3.13 | resolved in MW0 integration |
| OI-M01-001 | OPEN | Model gateway | Численные лимиты Plus для SIWC? | неизвестны; 429 → пауза и переключение на GLM реализованы в M11 | M61 |
| OI-M01-002 | OPEN | Model gateway / terms | Можно ли использовать план в многопользовательской MIA? | только локально/у владельца; нужна заявка OpenAI и ручное чтение terms | before hosted pilot (M62) |
| OI-M01-003 | OPEN | Model gateway latency | Safety buffering и более быстрая модель (`gpt-6-luna`)? | не проверено | M11, M61 |
| OI-WEB-001 | RESOLVED | Accounts / credentials | Постоянные web-идентичности и подключения моделей | M10 identity + M11 vault | resolved in MW1 integration |
| OI-WEB-005 | OPEN | Workspace / app identity | Постоянное состояние установок и identity приложений | состояние — M10 (resolved); SDK grants — M50 | M50 |
| OI-M10-001 | OPEN | Web data migration | Как перенести hub-аккаунты POC без Google `sub`? | скрипт пропускает их; нужна `--identity-map` | до M31/M62 |
| OI-M10-002 | OPEN | Web cookie | Настраивать ли `Secure` и trusted origins cookie из Settings? | same-origin деплой | M30/M62 |
| OI-M11-001 | OPEN | Model gateway | Принимает ли план structured output (`text.format`)? | нет; M12 делит ответ эвристикой | M61 |
| OI-M11-002 | OPEN | Model gateway | Вход в ChatGPT, когда сервер удалённый? | вход только на хосте backend | M62 |
| OI-M11-003 | OPEN | Credentials | Custody master key / KMS | ключи в секрет-менеджере деплоя → env | before public beta |
| OI-M12-001 | OPEN | Runs | Retention `runs`/`run_events` | хранятся бессрочно | M17 |
| OI-M12-002 | OPEN | Runs / web | Черновик текста в structured mode | без черновиков | M30 |
| OI-M12-003 | OPEN | Runs runtime | Runs при нескольких backend-инстансах | один инстанс | with OI-S18-002 |
| OI-M12-004 | OPEN | Voice | Язык и форма `speech.ack` | серверный текст «Секунду.» | M22 |
| OI-M20-001 | RESOLVED | Android assistant | Отдельная activity для ассистента? | `MainActivity` в отдельной assistant-задаче | resolved in M20 device gate |
| OI-M20-002 | OPEN | Android assistant | Заглушка RecognitionService в VIS | заглушка до M22 | M22 |
| OI-M20-003 | RESOLVED | Android assistant | ROLE_ASSISTANT запрашивается диалогом? | нет в One UI; открываются Настройки | resolved in M20 |
| OI-M21-001 | OPEN | Android STT | Калиброванная уверенность STT | эвристика | M22 |
| OI-M21-002 | DECIDED | Android STT | Политика загрузки модели ~225 МБ | предлагать загрузку по согласию пользователя в любой сети, без требования Wi-Fi | M22; Play Asset Delivery — до релиза |
| OI-M21-003 | OPEN | Android STT | Названия сервисов в смешанной речи RU/EN | как есть; словарь алиасов предложен | M22 |
| OI-M21-004 | OPEN | Android STT | WER на живой речи | измерено только на синтезе | M22 device gate |
| OI-M21-005 | OPEN | Android STT | Какие ABI упаковывать | только arm64-v8a | release AAB |

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

- Status: SUPERSEDED
- Source session: architecture
- Component: wake word
- Question: какая phrase является продуктовой и собирается для KWS evaluation?
- Why non-blocking: tap-to-talk и временная тестовая phrase позволяют завершить voice pipeline.
- Current assumption: phrase выбирается до S27.
- Options: «Привет, Мия»; другой брендовый вариант.
- Recommendation: короткая фонетически отличимая phrase, проверенная на RU/BY.
- Affects sessions: S27, S28.
- Revisit trigger/by: отложено вместе с activation scope (ADR-009).
- Resolution: SUPERSEDED as a PoC question on 2026-09-16. Wake-phrase (S27) and
  system-assistant (S28) activation are out of internal-beta scope per
  [ADR-009](../adr/0009-defer-wake-word-activation.md); «Привет, Мия» remains the
  provisional phrase in the disabled module. The phrase is decided only if
  activation returns to scope.
- Evidence: [ADR-009](../adr/0009-defer-wake-word-activation.md), S27 handoff,
  `core/wake/evaluation/README.md`.

### OI-ARCH-004 — First external tool

- Status: RESOLVED
- Source session: architecture
- Component: tools
- Question: нужен ли weather provider уже в PoC?
- Why non-blocking: system.get_current_datetime доказывает function calling без внешней зависимости.
- Current assumption: S17 начинает с system.get_current_datetime.
- Options: system time; weather with an adapter.
- Recommendation: time first, weather as follow-up.
- Affects sessions: S17, S32.
- Revisit trigger/by: S17.
- Resolution: S17 выбрала и реализовала `system.get_current_datetime`; weather
  отложен до отдельного adapter/scope решения. W4 integrator подключил один
  allow-list/executor к text и realtime; write tools не включены.
- Evidence: S17 handoff, `tests/tools/test_executor.py`,
  `tests/conversations/test_conversation_api.py`, `tests/realtime/test_gateway.py`,
  integrated `app/composition.py` at `e9ca3f1`.

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

- Status: DECIDED
- Source session: architecture
- Component: cost controls
- Question: какие session duration/daily audio/token limits достаточны для internal beta?
- Why non-blocking: config может начать с консервативных limits.
- Current assumption: one live session/user, short idle timeout, daily cap.
- Options: fixed limits; environment-specific limits.
- Recommendation: environment-specific values, measured in S31.
- Affects sessions: S16, S18, S31.
- Revisit trigger/by: S31 с измеренным реальным расходом.
- Resolution: DECIDED in S18 — консервативные, переопределяемые через окружение
  admission defaults (`MAX_DAILY_TEXT_TURNS`, `MAX_DAILY_LIVE_SESSIONS`,
  `MAX_ACTIVE_LIVE_SESSIONS_PER_USER`, `MAX_LIVE_SESSION_SECONDS`), circuit
  breakers и kill switches. Это прокси по числу запросов, а не token/audio
  бюджеты; S31 измеряет расход и может их заменить (OI-S18-003).
- Evidence: S18 handoff и его блок в этом файле, `app/core/limits.py`,
  `tests/hardening/test_admission_and_resilience.py`.

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

- Status: DECIDED
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
- Resolution: backend and S21 Android client use explicit reauthentication
  after an ambiguous refresh response; the old refresh token is never retried.
  Keep DECIDED until S30 lost-response E2E on a real authenticated client.
- Evidence: `app/modules/auth/README.md`, `tests/auth/test_service_postgres.py`,
  S21 handoff and `core/auth` unit tests at Android `83bc6c2`.

## S14 inbox

<!-- S14 adds OI-S14-* records here. -->

S14 completed with no new non-blocking product questions. Runtime mounting and
S12 bearer composition are assigned to the W3 integrator in the S14 handoff;
the session's REST/SSE lifecycle, ownership, retry, and disconnect gates pass.

## S16 inbox

### OI-S16-001 — Own the Live transport dependency during hardening

- Status: RESOLVED
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
Keystore device gate passed in S21-R1. Missing public OAuth/backend setup for
real-device sign-in and unreachable post-sign-in logout UI are blockers recorded
in the S21-R1 handoff, not OPEN_ISSUES entries.

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

No new non-blocking questions. S26-R1 completed the physical-device latency
gate at 83 ms speech-to-flush and 84 ms speech-to-cancel-send; see its handoff.

## S18 inbox

<!-- S18 adds OI-S18-* records here. -->

Decision updates from S18 hardening (the original records above are unchanged):

- OI-ARCH-006 → DECIDED for internal beta. The backend now enforces conservative,
  environment-overridable admission defaults: `MAX_DAILY_TEXT_TURNS=100`,
  `MAX_DAILY_LIVE_SESSIONS=20`, `MAX_ACTIVE_LIVE_SESSIONS_PER_USER=1`,
  `MAX_LIVE_SESSION_SECONDS=900`, provider circuit breakers and the
  `VOICE_ENABLED`/`TEXT_ENABLED` kill switches. These are request-count proxies,
  not token or audio-second budgets; S31 measures real spend and may replace them.
  Evidence: `app/core/config.py`, `app/core/limits.py`, `ops/README.md`,
  `tests/hardening/test_admission_and_resilience.py`.
- OI-S16-001 → RESOLVED as recommended: `websockets==15.0.1` is now a direct
  project dependency with a regenerated lockfile; `google-genai==1.10.0` and the
  isolated wire transport are unchanged, so no SDK migration was attempted.
  Evidence: `pyproject.toml`, `uv.lock`, `sh ops/security_scan.sh`.
- OI-S22-001 → assessed, still OPEN. S18 changed no REST contract: a final-page
  sync cursor is an API decision for S23/S30 with measured history cost, and
  internal-beta histories stay small (the daily text budget bounds growth).
  Nothing in hardening blocks adding it later.

### OI-S18-001 — Access/refresh signing key rotation

- Status: OPEN
- Source session: S18
- Component: backend authentication secrets
- Question: how should `AUTH_JWT_SECRET` be rotated without signing everyone out?
- Why non-blocking: internal beta can rotate in a maintenance window; every user
  simply re-authenticates, which the S21 client already handles.
- Current assumption: rotation is a planned re-login event, not a live operation.
- Options: keep single-secret rotation with forced re-login; add a verification-only
  previous-secret window plus a separate refresh-hash pepper.
- Recommendation: before external beta, split the refresh-token HMAC pepper from
  the access-token signing key, then add a previous-key verification window. The
  same secret currently signs access tokens and hashes stored refresh tokens, so a
  naive rotation also invalidates every stored refresh hash.
- Affects sessions: S30, S31, S33.
- Revisit trigger/by: before external beta or the first credential incident.
- Resolution:
- Evidence: `app/modules/auth/tokens.py`, S12 handoff known limitations.

### OI-S18-002 — Single-instance realtime and limit state

- Status: OPEN
- Source session: S18
- Component: backend scaling and recovery
- Question: when do realtime claims, active-session limits, circuit breakers and
  metrics move out of process memory?
- Why non-blocking: internal beta runs one backend instance and `ops/README.md`
  states that as a deployment rule; daily budgets already live in Redis.
- Current assumption: one worker/instance; abrupt process death clears claims and
  in-memory counters, which frees the user instead of stranding them.
- Options: keep process-local state; move claims and limits to Redis leases; add
  sticky routing or a session ownership registry.
- Recommendation: move claims and the active-session limit to Redis leases at the
  same time as the second instance; scrape metrics per instance until then.
- Affects sessions: S31, S33.
- Revisit trigger/by: first horizontal scale-out or measured restart impact.
- Resolution:
- Evidence: `app/modules/realtime/sessions.py`, `app/core/metrics.py`, S14 handoff.

### OI-S18-003 — Live idle timeout and token/audio budgets

- Status: OPEN
- Source session: S18
- Component: cost controls
- Question: should the backend also enforce a WebSocket idle timeout and true
  token/audio-second budgets, as architecture section 14 lists?
- Why non-blocking: the 900-second duration cap, one active session per user and
  the daily session count already bound provider exposure per user and day.
- Current assumption: no idle timeout; the realtime contract does not require a
  client ping, so a silent open session is bounded only by the duration cap.
- Options: keep duration-only caps; add an idle deadline derived from the
  advertised heartbeat interval; meter provider `usage.updated` into Redis budgets.
- Recommendation: meter `usage.updated` tokens and audio milliseconds during S31
  measurements, then agree the idle deadline with the Android client.
- Affects sessions: S31, S32, S33.
- Revisit trigger/by: S31 provider measurements.
- Resolution:
- Evidence: `app/modules/realtime/gateway.py`, `contracts/realtime/v1/README.md`.

## S23 inbox

No new non-blocking questions. The existing OI-S22-001 full-history traversal
assumption remains valid for this PoC; S23 introduces no new API decision.

## S27 inbox

<!-- S27 adds OI-S27-* records here. -->

No new non-blocking S27 issue. The provisional phrase remains governed by
OI-ARCH-003; missing independent corpus and device measurements are completion
gates recorded in the S27 handoff, not open issues.

## W5 integration inbox

<!-- Wave W5 integrator adds OI-W5-* records here. -->

### OI-W5-001 — Wake enrollment templates after sign-out

- Status: OPEN
- Source session: W5 integration
- Component: Android wake word / privacy
- Question: должен ли logout очищать локальные spectral-шаблоны enrollment
  (`WakeConfiguration.clearEnrollment()`), или они переживают смену аккаунта?
- Why non-blocking: интеграция выключает wake при logout, wake выключен по
  умолчанию, шаблоны лежат только в app-private storage и не уходят в сеть.
- Current assumption: logout снимает переключатель wake, но шаблоны остаются.
- Options: (a) очищать при logout; (b) оставлять и переиспользовать при
  повторном входе того же пользователя; (c) хранить шаблоны на пользователя.
- Recommendation: (a) для общего устройства, если beta допускает несколько
  аккаунтов; иначе (b) до появления реального multi-user требования.
- Affects sessions: S28, S30, S33.
- Revisit trigger/by: before shared-device beta.
- Resolution: —
- Evidence: `app/src/main/kotlin/by/mia/assistant/WakeMicrophoneArbiter.kt`,
  `core/wake/.../WakeConfiguration.kt`.

### OI-W5-002 — Pending operation for a server-deleted conversation

- Status: OPEN
- Source session: W5 integration
- Component: Android chat / durable pending operations
- Question: как пользователю отбросить durable pending-операцию, если
  conversation удалён на сервере?
- Why non-blocking: UI честно показывает сбой и предлагает Retry; данные не
  теряются, дубликаты не создаются благодаря idempotency key.
- Current assumption: операция остаётся retryable бесконечно.
- Options: (a) discard-действие в UI; (b) автоматическая очистка по коду ошибки
  сервера; (c) TTL на pending-операции.
- Recommendation: (b) с явным кодом ошибки, затем (a) как ручной выход.
- Affects sessions: S30, S32, S33.
- Revisit trigger/by: S30 text E2E.
- Resolution: —
- Evidence: S23 handoff known limitations, `AppChatDataSource.kt`.

## S28 inbox

<!-- S28 adds OI-S28-* records here. -->

## S30 inbox

### OI-W6-006 — Composition has no injection seam for the verifier and text provider

- Status: OPEN
- Source session: S30
- Component: backend composition
- Question: должен ли `install_runtime_api` принимать явные overrides для
  `GoogleIdTokenVerifier` и text provider, как `install_auth` уже принимает
  `service=`/`cache=`?
- Why non-blocking: full-stack тесты работают через monkeypatch атрибутов модуля.
- Current assumption: monkeypatch `app.modules.auth.composition.GoogleIdTokenVerifier`
  и `app.composition.FakeAIProvider`.
- Options: (a) необязательные параметры override; (b) фабрики в Settings;
  (c) оставить monkeypatch.
- Recommendation: (a). Переименование импорта сейчас ломает тесты молча и
  в опасную сторону — в сторону реального обращения к Google.
- Affects sessions: S18, S33.
- Revisit trigger/by: S33.
- Resolution: —
- Evidence: `tests/e2e/test_auth_text_journey.py`.

### OI-W6-007 — Access-token TTL floor blocks configuration-driven expiry tests

- Status: OPEN
- Source session: S30
- Component: backend auth configuration
- Question: нужен ли более низкий пол `auth_access_token_ttl_seconds`
  (сейчас `ge=600`) для test/dev окружений?
- Why non-blocking: истёкший токен выпускается напрямую через `TokenManager`.
- Current assumption: пол остаётся 600 s во всех окружениях.
- Options: (a) пол зависит от `app_env`; (b) оставить как есть.
- Recommendation: (b) до появления второго потребителя; зафиксировать причину.
- Affects sessions: S33.
- Revisit trigger/by: S33.
- Resolution: —
- Evidence: `app/core/config.py`, `tests/e2e/test_auth_text_journey.py`.

## S31 inbox

### OI-W6-002 — Turn attribution for a transcript that precedes the commit

- Status: OPEN
- Source session: S31 (via S16-R1)
- Component: Gemini Live mapping
- Question: какому turn_id принадлежит реплика, которую Gemini расшифровал до
  того, как клиент закоммитил следующий turn?
- Why non-blocking: события доходят и корректно отображаются; расходится только
  корреляция.
- Current assumption: приписывается `_last_state`, то есть предыдущему turn.
- Options: (a) буферизовать до commit; (b) выдавать предварительный turn_id;
  (c) оставить как есть и задокументировать.
- Recommendation: (a), если голосовая история будет группироваться по turn.
- Affects sessions: S31-R1, S33.
- Revisit trigger/by: когда голосовые turn попадут в историю.
- Resolution: —
- Evidence: `app/providers/ai/gemini_live/mapping.py` `_input_state`.

### OI-W6-003 — Provider failures are indistinguishable in the logs

- Status: OPEN
- Source session: S31 (via S16-R1)
- Component: Gemini Live adapter
- Question: как отличить дефект mapping от сетевого сбоя, не включая
  временную инструментацию?
- Why non-blocking: пользователь получает корректный retryable-сбой.
- Current assumption: `_receive_loop` ловит `Exception` и теряет причину;
  наружу идёт «provider unavailable».
- Options: (a) логировать только тип исключения (как это уже делает
  `JsonFormatter`); (b) счётчик по классу ошибки; (c) оставить.
- Recommendation: (a) — тип класса это серверный код, не содержимое.
- Affects sessions: S18, S33.
- Revisit trigger/by: before beta.
- Resolution: —
- Evidence: `app/providers/ai/gemini_live/adapter.py` `_receive_loop`.

### OI-W6-004 — Gemini Live model compatibility

- Status: DECIDED
- Source session: S31
- Component: Gemini Live provider
- Question: какая Live-модель поддерживается адаптером S16?
- Why non-blocking: рабочая модель найдена и зафиксирована.
- Current assumption: `gemini-2.5-flash-native-audio-latest`.
- Options: —
- Recommendation: закреплять модель явно и перепроверять адаптер при смене.
- Affects sessions: S31-R1, S33.
- Revisit trigger/by: при смене Live-модели.
- Resolution: измерено против живого API: `gemini-3.8-live` подключается, но не
  отвечает; `gemini-3.1-flash-live-preview` не подключается;
  `gemini-2.5-flash-native-audio-latest` даёт полный ответ (27 control +
  45 audio событий). Более новые Live-модели не являются drop-in заменой.
- Evidence: [S16-R1](handoffs/S16-R1.md).

### OI-W6-005 — Barge-in latency after the playback change

- Status: OPEN
- Source session: S31 (via S25-R1)
- Component: Android voice
- Question: сохраняются ли 83/84 ms speech-to-flush и speech-to-cancel-send
  после того, как playback-путь стал приостанавливаться?
- Why non-blocking: путь отмены не менялся; изменилось только поведение
  переполненного буфера.
- Current assumption: цифры S26-R1 всё ещё ориентир, но не подтверждены.
- Options: (a) перезапустить `BargeInDeviceTest`; (b) измерить против
  реального облачного ответа.
- Recommendation: (b) — S26-R1 измерял инъецированное аудио и fake gateway.
- Affects sessions: S31-R1, S33.
- Revisit trigger/by: S31-R1.
- Resolution: —
- Evidence: [S25-R1](handoffs/S25-R1.md), [S26-R1](handoffs/S26-R1.md).

### OI-W6-008 — Silence ends a Live session as a provider failure

- Status: RESOLVED
- Source session: S31
- Component: Gemini Live adapter
- Question: должна ли тишина в 90 s завершать Live-сессию с кодом
  `realtime_provider_unavailable`?
- Why non-blocking: tap-to-talk сессии короткие, а ошибка retryable.
- Current assumption: `idle_timeout_seconds = 90` применяется к любому
  молчанию провайдера, включая нормальное ожидание речи пользователя.
- Options: (a) считать idle timeout нормальным завершением; (b) keepalive;
  (c) увеличить таймаут.
- Recommendation: (a) с явным закрытием, иначе ассистент «ломается», пока
  пользователь думает.
- Affects sessions: S31-R1, S33.
- Revisit trigger/by: S31-R1.
- Resolution: RESOLVED в [S15-R1](handoffs/S15-R1.md). «Простой» теперь означает
  тишину в обе стороны: пока клиент шлёт кадры микрофона, сессия живёт. На
  устройстве сессия продержалась 180 s без ошибки.
- Evidence: [S15-R1](handoffs/S15-R1.md).

### OI-W6-009 — An abandoned Live session holds the only slot

- Status: RESOLVED
- Source session: S31
- Component: realtime admission
- Question: как освободить слот `MAX_ACTIVE_LIVE_SESSIONS_PER_USER`, если
  приложение убито и сессия не закрыта штатно?
- Why non-blocking: слот освобождается сам по idle timeout.
- Current assumption: пользователь ждёт до 90 s перед новой голосовой сессией.
- Options: (a) вытеснять старую сессию при новом connect того же пользователя;
  (b) короткий keepalive от клиента; (c) оставить.
- Recommendation: (a) — при cap в одну сессию это выглядит как зависание.
- Affects sessions: S31-R1, S18, S33.
- Revisit trigger/by: S31-R1.
- Resolution: RESOLVED в [S15-R1](handoffs/S15-R1.md) через (b)-подобный
  серверный вариант: сокет, молчащий в обе стороны 30 s, закрывается кодом 1001,
  и слот освобождается. Исходящий трафик считается активностью, поэтому клиент,
  слушающий ответ, не отключается.
- Evidence: [S15-R1](handoffs/S15-R1.md); повторный старт сразу после убийства
  приложения прошёл.

## S13-R1 inbox

### OI-W6-013 — Should the neutral tool contract carry a provider continuation token?

- Status: RESOLVED
- Source session: S13-R1
- Component: tool contract
- Question: нужно ли `ToolCallRequested`/`ToolFeedback` нести непрозрачный
  provider-токен (для Gemini — `thought_signature`), чтобы ручной tool-loop
  работал на моделях 3.x?
- Why non-blocking: PoC закреплён на `gemini-2.5-flash`, где продолжение хода
  принимается.
- Current assumption: нейтральный контракт не несёт provider-специфичных полей.
- Options: (a) непрозрачное поле, которое адаптер кладёт и возвращает;
  (b) хранить его в состоянии адаптера по call_id; (c) оставаться на 2.5.
- Recommendation: (b) — нейтральный контракт остаётся чистым, а знание
  провайдера живёт в провайдере.
- Affects sessions: S33 и следующий цикл планирования.
- Revisit trigger/by: когда потребуется модель 3.x.
- Resolution: MW1 integration (2026-10-09): вариант (a) в контракте M00 — `ModelToolCall.provider_state`/`ModelMessage.provider_state`; адаптеры M11 кладут туда reasoning items (Responses) и `reasoning_content` (GLM), run engine M12 возвращает их на следующем шаге. См. M11 inbox.
- Evidence: [S13-R1](handoffs/S13-R1.md), [M11](handoffs/M11.md), `tests/models/test_gateway_postgres.py`.

### OI-W6-014 — Credential-gated smokes have nowhere to run

- Status: RESOLVED
- Source session: S13-R1
- Component: verification
- Question: где выполнять smoke-тесты, требующие `GEMINI_API_KEY`?
- Why non-blocking: поведение проверено эквивалентным запуском того же пути
  внутри контейнера.
- Current assumption: тесты пропускаются везде; в production-образе нет pip,
  а ключ по политике недоступен host-процессу.
- Options: (a) отдельный test-образ с dev-группой; (b) запуск в managed
  окружении, где есть ключ; (c) оставить как документированный ручной шаг.
- Recommendation: (a) — иначе S16 Live smoke и эти два никогда не выполнятся.
- Affects sessions: S18, S33.
- Revisit trigger/by: S33.
- Resolution: RESOLVED в [S18-R1](handoffs/S18-R1.md) вариантом (a). Live smoke
  впервые реально выполнился и прошёл против настоящего API. Остаётся вопрос,
  кто запускает его в CI — [OI-W6-018](#oi-w6-018--who-runs-the-credential-smoke).
- Evidence: [S18-R1](handoffs/S18-R1.md).

## S15-R1 inbox

### OI-W6-015 — Client inactivity window is a module constant

- Status: OPEN
- Source session: S15-R1
- Component: realtime gateway
- Question: должно ли окно неактивности клиента (30 s) настраиваться оператором?
- Why non-blocking: значение с запасом больше любой нормальной паузы клиента, а
  исходящий трафик считается активностью.
- Current assumption: `_CLIENT_INACTIVITY_SECONDS = 30.0` в модуле.
- Options: (a) переменная окружения рядом с прочими admission-настройками;
  (b) оставить константой.
- Recommendation: (a) вместе со следующей ревизией ops-настроек.
- Affects sessions: S18, S33.
- Revisit trigger/by: S33.
- Resolution: —
- Evidence: [S15-R1](handoffs/S15-R1.md).

### OI-W6-016 — The client does not notice a closed socket while waiting

- Status: RESOLVED
- Source session: S15-R1
- Component: Android voice
- Question: почему экран остаётся в состоянии «waiting» после того, как гейтвей
  закрыл сессию?
- Why non-blocking: пользователь может выйти с экрана и начать заново.
- Current assumption: обработка закрытия сокета не покрывает
  `WaitingForResponse`.
- Options: (a) переводить в retryable-ошибку по закрытию в любом состоянии;
  (b) таймаут ожидания ответа на клиенте.
- Recommendation: (a), плюс (b) как страховка.
- Affects sessions: S31-R1.
- Revisit trigger/by: S31-R1.
- Resolution: RESOLVED в [S25-R2](handoffs/S25-R2.md). Контроллер был ни при чём:
  он уже умел превращать `Closed` в retryable-ошибку, но события не получал.
  OkHttp вызывает `onClosed` только после того, как клиент ответит на close-фрейм,
  а слушатель не переопределял `onClosing` и не отвечал. Вариант (b) не
  потребовался.
- Evidence: [S25-R2](handoffs/S25-R2.md).

## S16-R2 inbox

### OI-W6-017 — Barge-in now has two mechanisms

- Status: OPEN
- Source session: S16-R2
- Component: Android voice / Gemini Live
- Question: как взаимодействуют локальное перебивание (flush + `response.cancel`)
  и provider-side interrupt от `activityStart` при `START_OF_ACTIVITY_INTERRUPTS`?
- Why non-blocking: оба ведут к нужному результату — старый ответ прекращается.
- Current assumption: клиент глушит воспроизведение локально, а провайдер
  останавливает генерацию при открытии новой активности.
- Options: (a) измерить оба и оставить; (b) полагаться только на provider-side;
  (c) полагаться только на локальный.
- Recommendation: (a). До замеров нельзя называть ни одну цифру barge-in
  латентности: 83/84 ms из S26-R1 получены на другом протоколе.
- Affects sessions: S31-R1, S33.
- Revisit trigger/by: S31-R1.
- Resolution: —
- Evidence: [S16-R2](handoffs/S16-R2.md), [S26-R1](handoffs/S26-R1.md).

## S18-R1 inbox

### OI-W6-018 — Who runs the credential smoke

- Status: OPEN
- Source session: S18-R1
- Component: CI / release process
- Question: должен ли credential smoke выполняться автоматически, и где взять
  для этого ключ?
- Why non-blocking: скрипт документирован и запускается вручную одной командой.
- Current assumption: ручной шаг перед релизом и после правок провайдера.
- Options: (a) отдельный CI job с секретом; (b) запуск в staging-окружении, где
  ключ уже есть; (c) оставить ручным.
- Recommendation: (b) — не требует раздавать ключ CI-раннерам.
- Affects sessions: S33.
- Revisit trigger/by: S33.
- Resolution: —
- Evidence: [S18-R1](handoffs/S18-R1.md), `ops/README.md`.

## S25-R2 inbox

### OI-W6-019 — Voice sessions sometimes end on their own

- Status: RESOLVED
- Source session: S25-R2
- Component: Android voice / realtime
- Question: почему голосовая сессия на устройстве иногда обрывается сама через
  7–45 секунд?
- Why non-blocking: сбой retryable, экран предлагает переподключиться, и
  воспроизводится он непостоянно: соседние прогоны держались 180 с.
- Current assumption: причина не установлена. Наблюдалось следующее:
  гейтвей **не** закрывал сокет (в одном случае `websocket_completed` вовсе без
  `close_code`), `_fail` на сервере не вызывался, а клиент сообщал
  `Could not interrupt the realtime response` в состоянии `Interrupting`.
  Последнее — симптом: `cancelResponse` возвращает результат `WebSocket.send`,
  а тот даёт false на уже закрывающемся сокете. То есть соединение умирало
  раньше перебивания, и причина его смерти неизвестна.
- Options: (a) залогировать закрытия сокета на клиенте (кодом и стороной);
  (b) проверить, не рвёт ли соединение `adb reverse`; (c) воспроизвести с
  настоящей речью, где barge-in идёт по нормальному пути.
- Recommendation: (c) вместе с (a). Все наблюдения сделаны на шуме, где
  локальный и облачный VAD ведут себя нетипично.
- Affects sessions: S31-R1.
- Revisit trigger/by: S31-R1.
- Resolution: RESOLVED в [S25-R3](handoffs/S25-R3.md) по рекомендации (a)+(c).
  Сокет закрывал сам клиент кодом 1013: [S25-R1](handoffs/S25-R1.md) ждал место
  в playback-буфере внутри `events.collect`, останавливая потребителя канала на
  32 события, а Gemini льёт ответ быстрее, чем тот играет. Сбой barge-in был
  симптомом двумя шагами ниже. Аудио ответа теперь ждёт в собственной очереди.
- Resolution: —
- Evidence: [S25-R2](handoffs/S25-R2.md); backend `websocket_completed`
  duration 7593 ms без close_code и 45222 ms с 1011.

## S32 inbox

### OI-W6-001 — No provider test used a real registry declaration

- Status: OPEN
- Source session: S32 (via S17-R1)
- Component: provider tests
- Question: как не допустить повторения дефекта, при котором зелёный provider
  suite ничего не говорит о совместимости с облаком?
- Why non-blocking: конкретный дефект исправлен и закрыт регрессией.
- Current assumption: фикстуры объявляют `parameters={"type": "object"}`.
- Options: (a) обязать каждый provider suite прогонять
  `current_datetime_registry().declarations()`; (b) contract-тест на маппинг;
  (c) оставить точечную регрессию.
- Recommendation: (a) — дефект был полностью невидим для фикстур.
- Affects sessions: S33.
- Revisit trigger/by: S33.
- Resolution: —
- Evidence: [S17-R1](handoffs/S17-R1.md).

### OI-W6-010 — The gateway re-cancels tool tasks at session close

- Status: OPEN
- Source session: S32
- Component: realtime gateway
- Question: стоит ли отменять tool-задачи повторно в teardown, вместо ожидания
  уже отменённых?
- Why non-blocking: executor теперь переживает повторную отмену.
- Current assumption: `gateway.py` отменяет задачи и в `response.cancel`,
  и в `finally`.
- Options: (a) отменять только не-`cancelling()` задачи; (b) полагаться на
  существующий `gather`; (c) оставить.
- Recommendation: (a) — отмена финализирующейся задачи это ловушка для любого
  будущего tool с cleanup.
- Affects sessions: S33.
- Revisit trigger/by: S33.
- Resolution: —
- Evidence: `app/modules/realtime/gateway.py` teardown; [S32](handoffs/S32.md).

### OI-W6-011 — A cancelled tool call is invisible to the client

- Status: OPEN
- Source session: S32
- Component: realtime contract
- Question: должен ли клиент получать `tool.call.cancelled`, или он обязан
  вывести отмену из `response.interrupted`?
- Why non-blocking: `ToolCallCancelled` уже существует в контракте AIEvent, а
  PoC-инструмент мгновенный.
- Current assumption: клиент видит только `response.interrupted`.
- Options: (a) публиковать событие отмены; (b) оставить вывод на клиенте.
- Recommendation: (a) до того, как появится долгий инструмент.
- Affects sessions: S33.
- Revisit trigger/by: перед первым долгим инструментом.
- Resolution: —
- Evidence: `app/providers/ai/base/events.py`.

### OI-W6-012 — A cancelled audit row carries a failure error code

- Status: OPEN
- Source session: S32
- Component: tool audit
- Question: корректно ли, что у записи со `status='cancelled'` стоит
  `error_code = 'tool_execution_failed'`?
- Why non-blocking: статус — источник истины, и он верен.
- Current assumption: код и статус расходятся.
- Options: (a) отдельный код отмены; (b) пустой код при отмене; (c) оставить.
- Recommendation: (b) — отмена это не сбой.
- Affects sessions: S33.
- Revisit trigger/by: S33.
- Resolution: —
- Evidence: `app/modules/tools/executor.py`.

## S33 inbox

<!-- S33 adds OI-S33-* records here. -->

## WEB-P1 inbox — new web platform

This direction was requested directly on 2026-10-05 and is tracked separately
from the old voice/session dependency graph. These issues do not change its gates.

### OI-WEB-001 — Persistent web identities and model credentials

- Status: RESOLVED
- Source session: WEB-P1
- Component: Web auth and provider connections
- Question: какой hosting/vault будет использоваться для постоянных BYOK-подключений?
- Why non-blocking: hosted Google accounts/workspaces уже постоянны; BYOK остаётся в памяти до 8h/restart и явно требует повторного ввода.
- Current assumption: WEB-P1-R2 переиспользует shared Google OAuth и собственную Secure/HttpOnly session; ключи не попадают в browser storage/SQLite. Vault/KMS ещё не реализован.
- Options: managed cloud vault; локальный companion vault; смешанная конфигурация.
- Recommendation: shared authenticated backend + managed vault, локальный inference через companion connector.
- Affects sessions: WEB-P2, WEB-P6.
- Revisit trigger/by: перед обещанием постоянного хранения API-ключей и публичной beta.
- Resolution: MW1 integration (2026-10-09), для MVP: identity — M10 (web cookie и Android bearer → один `users.id` по проверенному Google `sub`); подключения моделей — M11 (PostgreSQL, AES-256-GCM под per-user DEK, master key `CREDENTIALS_MASTER_KEYS`, ротация и crypto-shred). Custody master key — OI-M11-003; hub-аккаунты — OI-M10-001.
- Evidence: [M10](handoffs/M10.md), [M11](handoffs/M11.md).

### OI-WEB-002 — Reminder execution outside an open browser

- Status: OPEN
- Source session: WEB-P1
- Component: Scheduler / notifications
- Question: какие устройства должны выполнять будильники при закрытой вкладке и отсутствии сети?
- Why non-blocking: web POC явно ограничивает напоминания открытой вкладкой.
- Current assumption: shared server deadlines + browser notifications для POC; durable scheduler/push или device agent для продукта.
- Options: backend scheduler + push; native/column scheduler; оба с единой identity/idempotency.
- Recommendation: backend jobs для cloud-автоматизаций и локальный device scheduler для колонки.
- Affects sessions: WEB-P4, WEB-P6.
- Revisit trigger/by: перед background notifications и обещанием надёжного будильника.
- Resolution: —
- Evidence: `web/src/plugins/scheduler.ts`, `web/src/App.tsx`, `web/README.md`.

### OI-WEB-003 — Community publication and executable plugins

- Status: OPEN
- Source session: WEB-P1
- Component: Plugin store / SDK
- Question: когда открывать сторонним авторам workflow/UI runtime и публикацию версий?
- Why non-blocking: локальный manifest v2, ограниченные actions, iframe view и SDK уже работают; публичной публикации нет.
- Current assumption: перед public catalog нужны immutable versions, verified author/client identity, pinned grants и review; локальный HTML sandbox не является production gate.
- Options: только рецепты; declarative workflow; executable extension sandbox.
- Recommendation: recipe-first publication с grants/versioning, затем расширять runtime по фактическим потребностям.
- Affects sessions: WEB-P3, WEB-P5.
- Revisit trigger/by: до публичной публикации community-плагинов.
- Resolution: —
- Evidence: `docs/PLUGIN_DEVELOPMENT.md`, `contracts/plugins/v2/application.schema.json`, `packages/mia-sdk/`.

### OI-WEB-004 — Live verification of web provider accounts

- Status: OPEN
- Source session: WEB-P1
- Component: Provider verification
- Question: какие конкретные модели доступны реальным аккаунтам и локальной установке владельца?
- Why non-blocking: native adapter protocols и journeys проверены без paid calls; UI даёт editable model ID и явный «Тест API».
- Current assumption: model names в формах — примеры, credentials не были прочитаны и live web smoke не запускался.
- Options: ручной тест в UI; staging credential smoke после WEB-P2.
- Recommendation: проверить выбранные провайдеры через UI и позже добавить managed smoke suite.
- Affects sessions: WEB-P2.
- Revisit trigger/by: при первом подключении реальных моделей / до публичной beta.
- Resolution: —
- Evidence: `tests/web_poc/test_web_api.py`, `web/src/components/Models.tsx`.

### OI-WEB-005 — Durable installation state and app identity after runtime POC

- Status: OPEN
- Source session: WEB-P1-R1
- Component: Plugin runtime / SDK / persistence
- Question: как переносить локальную установку и привязанные приложения в постоянный аккаунт?
- Why non-blocking: hosted Google accounts/installations/state/layout уже сохраняются в SQLite и изолированы; SDK grants остаются временными.
- Current assumption: WEB-P1-R2 сохраняет installations/state/layout/reviews аккаунта после restart. SDK grants остаются в памяти до часа; connection keys до 8h. Анонимные локальные данные не переносятся в Google-аккаунт автоматически.
- Recommendation: продолжить WEB-P2: явная миграция локальных данных, PostgreSQL/vault и production PKCE/device grants. Не переносить временные bearer tokens автоматически.
- Affects sessions: WEB-P2, WEB-P6.
- Revisit trigger/by: до хранения важных пользовательских данных и подключения устройств.
- Resolution: MW1 integration (2026-10-09): часть «постоянное состояние» RESOLVED в M10 (installations, plugin state, раскладка с `expected_version`, отзывы в PostgreSQL, перенос из SQLite). Часть «app identity / SDK grants» остаётся OPEN для M50.
- Evidence: [WEB-P1-R1](handoffs/WEB-P1-R1.md), [M10](handoffs/M10.md).

### OI-WEB-006 — Shared-server VPN/SSH access

- Status: RESOLVED
- Source session: WEB-P1-R2
- Component: Deployment
- Question: разрешено ли операционное использование credentials для подключения к серверу?
- Current assumption: пользователь разрешил сохранённые env values только как переменные, без просмотра/вывода. Wrapper загрузил deployment environment с отключённым tracing и подавленными диагностическими сообщениями; `SERVER_PASSWORD` передан shared authentication tool через окружение, grep fallback не использовался.
- Affects sessions: WEB-P1-R2 publication only; old Sxx gates unchanged.
- Resolution: 2026-10-06 VPN/SSH connection, checksum-verified image upload and isolated MIA release completed. Public health/auth/401/assets/screenshots/hub-card checks PASS; actual Chromium hub → login → Google OAuth redirect PASS; neighboring applications/uptimes preserved.
- Evidence: user authorization, `deploy/README.md`, `deploy/publish-via-shared-server.sh`, `docs/implementation/handoffs/WEB-P1-R2.md`, `docs/previews/mia-live-hub.png`.

### OI-WEB-007 — Google return rejected by Fetch Metadata boundary

- Status: RESOLVED
- Source session: WEB-P1-R3
- Component: Hosted web middleware / Google return
- Problem: Google return to `/mia/?signin=google` was rejected as cross-site with 403; callback is already configured on the shared server.
- Resolution: allow only top-level GET/HEAD static entry navigation; retain origin/metadata checks on API, mutations, iframe and fetch requests. Regression FAIL before / PASS after; 71 backend tests PASS. Published 2026-10-06; real Chromium cross-site 303 return now 200/UI loaded; API cross-site 403 and anonymous workspace 401 remain. Container healthy, existing apps/cards/uptimes preserved.
- Evidence: `tests/web_poc/test_accounts.py`, `web/scripts/verify-hosted-auth-return.mjs`, `docs/implementation/handoffs/WEB-P1-R3.md`.


## MVP M-plan inboxes

Каждая M-сессия пишет только в свой блок (ID `OI-Mxx-NNN`). Карта закрытия существующих вопросов — `MVP_SESSION_PLAN.md` §8.

## M00 inbox

### OI-M00-001 — Реестр кодов ошибок для mvp/v1

- Status: RESOLVED
- Source session: M00
- Component: Contracts / errors
- Question: где регистрировать новые публичные коды ошибок MVP (`approval_args_mismatch`, `approval_expired`, `model_not_configured`, `unsupported_tool` и т. п.)?
- Why non-blocking: `common.schema.json#/$defs/publicError` и `ErrorEnvelope` в `openapi.yaml` принимают любой код `^[a-z][a-z0-9_]{1,63}$`; коды перечислены в Literal-типах `app/core/ports/*`.
- Current assumption: старый реестр `contracts/errors/v1` не меняется; новые коды фиксируются в handoff сессии, которая их вводит.
- Options: аддитивно расширить `errors/v1/error-codes.json`; завести `contracts/mvp/v1/error-codes.json` и проверять его валидатором; оставить pattern.
- Recommendation: M12 заводит `contracts/mvp/v1/error-codes.json` (аддитивно, с проверкой в валидаторе), M13/M50 дописывают свои коды.
- Affects sessions: M12, M13, M16, M17, M50.
- Revisit trigger/by: первая сессия MW1, отдающая ошибки через `/v1/*`.
- Resolution: MW1 integration (2026-10-09): реестр `contracts/mvp/v1/error-codes.json` (M12) с проверкой `validate_mvp_error_codes`; каждая сессия дописывает свои коды. Коды M10 (`version_conflict`, `tools_digest_mismatch`, …) и API M11 ещё не внесены — follow-up в STATUS (MW1).
- Evidence: [M12](handoffs/M12.md), `contracts/scripts/validate_contracts.py`.

### OI-M00-002 — Категории памяти и `scope` фактов предварительные

- Status: OPEN
- Source session: M00
- Component: Memory Ledger
- Question: окончательный набор категорий (`profile`, `work`, `home`, `health`, `finance`, `interests`, `other`) и значения `Fact.scope` (`user`, `automation`).
- Why non-blocking: набор закреплён в `memory.schema.json` и `MemoryCategory`; расширение аддитивно, scopes приложений `memory.read:<category>` следуют ему.
- Current assumption: категории как в схеме; `automation` готовит «профиль автоматизаций» V2 (Review §7.3).
- Options: оставить; переименовать до первых данных; сделать категории пользовательскими.
- Recommendation: M17 подтверждает или аддитивно расширяет до миграции данных.
- Affects sessions: M17, M50, M31.
- Revisit trigger/by: начало M17.
- Resolution:
- Evidence:

### OI-M00-003 — Версия Python окружения разработки

- Status: RESOLVED
- Source session: M00
- Component: Backend tooling
- Question: какую версию Python закрепить для `uv sync`?
- Why non-blocking: при `uv sync --python 3.13` все проверки проходят; Docker-образы уже на 3.13.
- Current assumption: на машине разработчика `uv sync` без флага выбирает Python 3.14, и SQLAlchemy 2.0.40 падает на импорте (`TypeError` в `make_union_type`) — весь pytest не собирается.
- Options: добавить `.python-version` с `3.13`; сузить `requires-python` до `>=3.12,<3.14`; обновить SQLAlchemy.
- Recommendation: integrator MW0 добавляет `.python-version` (`3.13`) в корень — общий файл вне scope M00.
- Affects sessions: все backend-сессии MW1+ (M10, M11, M12, M13…).
- Revisit trigger/by: интеграция MW0.
- Resolution: MW0 integration (2026-10-09) добавила корневой `.python-version` = `3.13`; `uv sync` без флага берёт 3.13.1, полный pytest 222 passed / 63 skipped.
- Evidence: локальный прогон M00 (handoff M00, раздел Verification).

## M01 inbox

### OI-M01-001 — Numeric ChatGPT Plus limits for SIWC plan usage

- Status: OPEN
- Source session: M01
- Component: Model Gateway / `chatgpt-plan`
- Question: каковы численные лимиты Plus (окно общее с ChatGPT) и как рано их обнаруживать?
- Why non-blocking: ответы API не содержат заголовков лимитов (только `x-codex-turn-state`, `x-codex-safety-buffering-*`); исчерпание сообщается ошибкой `subscription_sharing_usage_limit_exceeded` (429 в `response.failed`), есть запасной провайдер GLM.
- Current assumption: хватает для одного разработчика; при 429 адаптер ставит паузу и переключается на GLM.
- Options: наблюдать на практике в M11/M61; показывать пользователю ссылку на ChatGPT settings → Usage.
- Recommendation: обработка 429 как паузы в M11, замер реального расхода в M61.
- Affects sessions: M11, M61.
- Revisit trigger/by: первый 429 в разработке / до публичной beta.
- Resolution: —

### OI-M01-002 — SIWC terms for a multi-user hosted MIA

- Status: OPEN
- Source session: M01
- Component: Product / legal
- Question: допустим ли SIWC plan usage, когда сервер MIA обслуживает не только владельца?
- Why non-blocking: документация покрывает open-source и локально размещённые приложения; текст условий (openai.com/policies/sign-in-with-chatgpt-terms) не удалось прочитать автоматически (HTTP 403). План используется только для подключённого приложения, без универсального API-доступа третьим сторонам.
- Current assumption: владелец сам хостит сервер и входит своим Plus; для платного/удалённого облака нужна заявка (interest form).
- Options: прочитать условия человеком; подать заявку OpenAI; для многопользовательского режима использовать BYOK/GLM.
- Recommendation: до любого многопользовательского запуска прочитать условия и подать заявку.
- Affects sessions: M11, M61.
- Revisit trigger/by: до приглашения других пользователей.
- Resolution: —

### OI-M01-003 — GPT-plan latency and safety buffering

- Status: OPEN
- Source session: M01
- Component: Model Gateway / performance
- Question: можно ли снизить first-token p50 (2.4 с на `gpt-6.1-sol`) выбором модели (`gpt-6-luna`)?
- Why non-blocking: задержка приемлема для чата; заголовки `x-codex-safety-buffering-enabled: true` и `…-faster-model: gpt-6-luna` намекают на буферизацию стрима; проверена одна модель, n=5.
- Current assumption: разница модели/буферизации даёт долю секунды-секунду.
- Options: прогнать `--model gpt-6-luna --model gpt-5.6-luna`; отдельный быстрый путь для голоса.
- Recommendation: замерить в M61 перед настройкой голосового пути.
- Affects sessions: M11, M61.
- Revisit trigger/by: M61.
- Resolution: —

### Resolution update — OI-WEB-004

- OI-WEB-004 (live verification of web provider accounts): частично закрыт для двух провайдеров. Живой прогон 2026-10-09: `zai-org/GLM-5.3-Flash` на DeepInfra доступен (tools и `reasoning_content` работают) и ChatGPT-plan `gpt-6.1-sol` доступен через SIWC (список из 7 видимых моделей в `spikes/m01/results/gpt-plan-20261009T014154Z.json`). Остальные провайдеры не проверялись. Статус OI-WEB-004 остаётся OPEN до managed smoke suite; закрывающее решение вносит интегратор по карте §8 плана.
- Evidence: `spikes/m01/DECISION.md`, `spikes/m01/results/`, `docs/implementation/handoffs/M01.md`.

## M10 inbox

### Resolution update → OI-WEB-001 (identity part)

- Proposed status: identity part RESOLVED by M10; BYOK/vault part stays OPEN for M11.
- Resolution: web и Android входят в одну запись `users` по проверенному Google `sub` (не email): `POST /v1/auth/web/{nonce,google,logout}` выдаёт HttpOnly `SameSite=Strict` cookie `mia_session` (в Postgres только HMAC, таблица `web_sessions`); `RequestAuthenticator` даёт одинаковый `user_id` для bearer и cookie. Хранение ключей моделей/подключений не входило в M10.
- Evidence: `app/modules/auth/principal.py`, `tests/auth/test_web_session_postgres.py::test_cookie_and_bearer_yield_the_same_user_and_workspace`, `docs/implementation/handoffs/M10.md`.

### Resolution update → OI-WEB-005 (persistence part)

- Proposed status: installations/state/layout/reviews part RESOLVED by M10; app identity / SDK grants part stays OPEN for M50.
- Resolution: таблицы `workspaces`, `installations`, `plugin_states`, `widget_instances`, `plugin_reviews` в Postgres (revision `20261009_m10_0001`), API `/v1/installations*`, `/v1/dashboard*` с `expected_version`/409. Перенос web-SQLite — явный скрипт `scripts/migrate_web_sqlite.py` (read-only источник, insert-if-absent, проверка владельца по `sub`, `--owner-sub`, `--dry-run`). Временные SDK grants, cookie-сессии, BYOK и анонимные данные не переносятся.
- Evidence: `app/modules/workspace/legacy_import.py`, `tests/workspace/test_legacy_import_postgres.py`.

### OI-M10-001 — Hosted web POC accounts from the hub have no Google `sub`

- Status: OPEN
- Source session: M10
- Component: Accounts / web data migration
- Question: как связать аккаунты hosted web POC, созданные через общий hub (`google_sub = "hub:<uuid>"`), с проверенным Google `sub`?
- Why non-blocking: скрипт переноса пропускает такие аккаунты (`unmapped_hub_identity`) и принимает явную карту `--identity-map {"hub:<uuid>": "<google sub>"}`; новый web-вход (M30) идёт напрямую через Google Identity Services и hub не использует.
- Current assumption: оператор строит карту из данных hub (если hub хранит Google `sub`) либо пользователь заново входит через Google и данные hub-аккаунта переносятся по его явному выбору.
- Options: карта из БД hub; endpoint hub, отдающий проверенный `sub`; не переносить hub-данные (пилот начинается с чистого аккаунта).
- Recommendation: проверить, хранит ли hub Google `sub`; если да — карта на один прогон перед M31 (удаление `web_poc`), иначе явный выбор пользователя.
- Affects sessions: M30, M31, M62.
- Revisit trigger/by: до удаления `app/web_poc` (M31) и пилотного деплоя.
- Resolution:
- Evidence: `app/web_poc/main.py` (`/api/web/auth/hub`), `app/modules/workspace/legacy_import.py`.

### OI-M10-002 — Web cookie policy is not configurable from Settings

- Status: OPEN
- Source session: M10
- Component: Configuration / web auth
- Question: нужны ли env-настройки `Secure`-флага и доверенных origins для cookie, если web и API окажутся на разных origin?
- Why non-blocking: `install_auth` берёт `WebCookiePolicy(secure = APP_ENV != "test")`, same-origin по `Host`; web и API в MVP обслуживаются с одного origin.
- Current assumption: same-origin deployment (`/mia/` за общим nginx).
- Options: добавить `WEB_COOKIE_SECURE`, `WEB_TRUSTED_ORIGINS` в `app/core/config.py`; оставить код-конфигурацию.
- Recommendation: integrator/M30 добавляет две настройки, если web выносится на другой origin.
- Affects sessions: M30, M62.
- Revisit trigger/by: выбор схемы деплоя web (M30/M62).
- Resolution:
- Evidence: `app/modules/auth/principal.py`, `app/modules/auth/composition.py`.

## M11 inbox

### Resolution update — OI-W6-013

- OI-W6-013 (нужен ли непрозрачный provider-токен в нейтральном контракте): RESOLVED. Выбран вариант (a): контракт M00 уже содержит `ModelToolCall.provider_state` / `ModelMessage.provider_state`; адаптеры M11 кладут туда reasoning-items Responses API с `encrypted_content` (`responses_reasoning_items`) и `reasoning_content` GLM и воспроизводят их дословно из сообщения. Run engine (M12) обязан сохранять `provider_state` вместе с tool call и возвращать его в `ModelMessage.tool_calls`; клиентам поле не показывается.
- Evidence: `app/modules/models/adapters/responses.py`, `adapters/chat_completions.py`; `tests/models/test_responses_adapter.py::test_encrypted_reasoning_is_kept_in_provider_state_and_replayed_verbatim`, `tests/models/test_chat_adapters.py::test_glm_tool_loop_replays_reasoning_content_from_provider_state`.

### Resolution update — OI-WEB-001

- OI-WEB-001 (hosting/vault для постоянных BYOK-подключений): RESOLVED для MVP. Постоянные подключения хранятся в PostgreSQL приложения; секреты запечатаны AES-256-GCM под per-user DEK, DEK обёрнут master key из конфигурации (`CREDENTIALS_MASTER_KEYS`), поддержаны ротация master key и crypto-shred при удалении DEK/пользователя. API не возвращает ключ (только последние 4 символа). Управляемый KMS/HSM и локальный companion connector остаются будущими опциями: custody master key вынесена в OI-M11-003.
- Evidence: `app/core/crypto.py`, `app/modules/models/vault.py`, `tests/models/test_vault_postgres.py`, `tests/models/test_api_postgres.py`.

### Progress notes — OI-M01-001, OI-M01-003

- OI-M01-001 (лимиты Plus): остаётся OPEN. Реализовано поведение из рекомендации: `subscription_sharing_usage_limit_exceeded` (HTTP 429 и событие в потоке) ставит подключение на паузу (Retry-After или `MODEL_USAGE_PAUSE_SECONDS`) и переключает шаг `mia:default` на запасного провайдера, если поток ещё ничего не отдал. Реальный 429 не наблюдался (живой smoke не запускался).
- OI-M01-003 (задержка, `gpt-6-luna`): остаётся OPEN, M11 не измерял; `python -m app.modules.models.smoke --model <slug>` пригоден для замеров в M61.

### OI-M11-001 — Structured output (speech/display JSON) on the plan endpoint

- Status: OPEN
- Source session: M11
- Component: Model Gateway / `chatgpt-plan`
- Question: принимает ли Responses-эндпоинт плана `text.format` (json_schema) для `assistant-response.schema.json`?
- Why non-blocking: шлюз не запрашивает structured output и отдаёт `supports_structured_output=false`; run engine использует splitter из ADR-0011.
- Current assumption: параметр может быть отвергнут (`subscription_sharing_unsupported_capability`), поэтому не отправляется.
- Options: проверить `text.format` живым запросом; оставить splitter.
- Recommendation: проверить в M12/M61, включить флаг в `ModelInfo` только после живой проверки.
- Affects sessions: M12, M61.
- Revisit trigger/by: контрактные тесты второй модели (M61).
- Resolution: —

### OI-M11-002 — Sign in with ChatGPT works only on the backend host

- Status: OPEN
- Source session: M11
- Component: Model Gateway / sign-in
- Question: как входить в ChatGPT, когда сервер MIA удалённый, а браузер пользователя на другой машине?
- Why non-blocking: redirect URI обязан быть `http://127.0.0.1:<port>/auth/callback` на машине с браузером; для владельца, который сам размещает сервер, это работает; для остальных есть ключи API (DeepInfra и др.).
- Current assumption: пилот — один оператор (OI-M01-002); состояние попытки входа хранится в памяти одного процесса.
- Options: локальный companion connector, который выполняет вход у пользователя и передаёт токены серверу; только BYOK для остальных.
- Recommendation: companion connector, если потребуется многопользовательский режим после решения OI-M01-002.
- Affects sessions: M30, M62.
- Revisit trigger/by: до приглашения пользователей (M62).
- Resolution: —

### OI-M11-003 — Master key custody and KMS

- Status: OPEN
- Source session: M11
- Component: crypto / operations
- Question: где хранить master keys и как автоматизировать ротацию (KMS/секрет-менеджер вместо переменной окружения)?
- Why non-blocking: `CREDENTIALS_MASTER_KEYS` из окружения процесса работает и обязательна в staging/production; ротация выполняется вручную через `KeyVault.rotate_master_key`.
- Current assumption: оператор хранит ключи в секрет-менеджере деплоя и подставляет в окружение.
- Options: облачный KMS как источник master key (envelope уже его допускает); плановая ротация jobs-сессией.
- Recommendation: KMS до публичной беты; ротацию добавить в операционный runbook.
- Affects sessions: M62.
- Revisit trigger/by: перед публичной бетой.
- Resolution: —

## M12 inbox

### OI-M12-001 — Retention of runs and run events

- Status: OPEN
- Source session: M12
- Component: Run engine / persistence
- Question: how long are `runs` and `run_events` (response text, tool `structured_content`, approval arguments) kept, including incognito runs?
- Why non-blocking: SSE resume only needs recent events; volume is small for one user; `input_text` of incognito runs is not stored and incognito runs never reach the feed.
- Current assumption: kept indefinitely; deleting the user cascades.
- Options: TTL job (e.g. 30 days, feed entries stay); delete events of finished runs after N days but keep `runs.response`; encrypt with the per-user DEK (ADR-0012) and crypto-shred.
- Recommendation: TTL on `run_events` with the M17 privacy work, before public beta.
- Affects sessions: M17, M31, M62.
- Revisit trigger/by: M17 or first retention policy decision.
- Resolution:
- Evidence: `migrations/versions/20261009_m12_runs.py`, handoff M12.

### OI-M12-002 — Draft text when the model answers in structured mode

- Status: OPEN
- Source session: M12
- Component: Run engine / chat UX
- Question: should chat show a streaming draft when the model returns the speech/display JSON?
- Why non-blocking: `message.delta` is emitted in plain-text mode; in structured mode the client gets `response.final` only, which is correct but slower to first pixel.
- Current assumption: no drafts in structured mode.
- Options: incremental extraction of `display.markdown` from partial JSON; plain-text mode for chat and structured only for voice; keep as is.
- Recommendation: decide after M11 measures first-token latency of the primary model and M30 builds the chat view.
- Affects sessions: M11, M30, M61.
- Revisit trigger/by: M30.
- Resolution:
- Evidence: `app/modules/runs/engine.py` (`model_step`).

### OI-M12-003 — Run ownership with more than one backend instance

- Status: OPEN
- Source session: M12
- Component: Run engine / runtime
- Question: how do live runs, cancel signals and SSE wake-ups work across several backend processes?
- Why non-blocking: internal beta runs one instance (OI-S18-002); the store is the source of truth, readers re-poll every 15 s, cancel via another instance is honoured at the next step boundary through `cancel_requested`, and a stale heartbeat (60 s) lets any instance end an orphaned run with `run_interrupted`.
- Current assumption: single instance.
- Options: Postgres `LISTEN/NOTIFY` or Redis pub/sub for wake-ups and cancel; run leases; sticky routing per run.
- Recommendation: Postgres `LISTEN/NOTIFY` when a second instance is introduced.
- Affects sessions: M62.
- Revisit trigger/by: before horizontal scaling (together with OI-S18-002).
- Resolution:
- Evidence: `app/modules/runs/service.py`, `notifier.py`.

### OI-M12-004 — Locale and form of the `speech.ack` filler

- Status: OPEN
- Source session: M12
- Component: Voice / run engine
- Question: should the filler follow the user's locale, vary, or be an on-device earcon?
- Why non-blocking: one deployment-wide phrase `RUN_SPEECH_ACK_TEXT` (default «Секунду.») after `RUN_VOICE_ACK_DELAY_MS` (1500); clients may play an earcon instead of `text`.
- Current assumption: Russian-speaking users, server text.
- Options: per-locale phrases; client-side earcon only; model-generated filler.
- Recommendation: M22 measures time to first sound on the phone and chooses.
- Affects sessions: M22, M61.
- Revisit trigger/by: M22.
- Resolution:
- Evidence: `contracts/mvp/v1/run-events.schema.json#/$defs/speechAck`.

### Resolution update — OI-M00-001

- M12 created `contracts/mvp/v1/error-codes.json` (`code`, `http_status` or null for event-only codes, `retryable`, `public_message`, `owner`) with a validator check (shape, uniqueness, `retryable` parity with `errors/v1`) and registered its 18 codes; `tests/runs/test_wiring.py` keeps `app/modules/runs/errors.py` in sync. M13/M16/M17/M50 append their codes. Suggested status: DECIDED (integrator updates the original record).
- Evidence: handoff M12, `contracts/scripts/validate_contracts.py::validate_mvp_error_codes`.

## M20 inbox

### OI-M20-001 — Нужна ли отдельная activity для запуска ассистента

- Status: RESOLVED
- Source session: M20
- Component: Android assistant
- Question: жест запускает `MainActivity` через `startAssistantActivity`; на SM-S938B нужно подтвердить, что она открывается в отдельной assistant-задаче поверх экрана и поверх блокировки, а не смешивается с обычной задачей приложения.
- Why non-blocking: поведение зависит от OEM/версии Android, физического устройства в сессии не было; логика запуска и lock-политика покрыты JVM-тестами.
- Current assumption: достаточно `MainActivity` (CLEAR_TOP | SINGLE_TOP | NEW_TASK, `setShowWhenLocked` динамически, снимается в `onStop`).
- Options: оставить `MainActivity`; завести `AssistantActivity` в `app` с `launchMode=singleInstance`, `showWhenLocked`, `excludeFromRecents`.
- Recommendation: сначала прогнать device gate по handoff M20; отдельная activity — только если проявятся смешение задач или утечка истории на заблокированном экране.
- Affects sessions: M22.
- Revisit trigger/by: device gate M20 на SM-S938B (или раньше MW1 gate).
- Resolution: `MainActivity` is enough: the assist launch opens it in its own `type=assistant` task, separate from the normal task, and it shows over a secure keyguard; back returns to the lock screen without exposing conversations.
- Evidence: device gate on SM-S938B, handoff M20 (`dumpsys activity activities`, `dumpsys window`).

### OI-M20-003 — Роль ассистента нельзя запросить диалогом, нужны Настройки

- Status: RESOLVED
- Source session: M20
- Component: Android assistant
- Question: `RoleManager.createRequestRoleIntent(ROLE_ASSISTANT)` работает?
- Why non-blocking: найдено и исправлено в device gate.
- Current assumption: нет; `RequestRoleActivity` завершается с «Role is not requestable».
- Options: открывать `Settings.ACTION_VOICE_INPUT_SETTINGS`.
- Recommendation: так и сделано (`59acb88`).
- Affects sessions: M22 (UX онбординга).
- Revisit trigger/by: —
- Resolution: экран ассистента открывает системную страницу «Цифровой помощник»; на One UI Mia в списке «Другие приложения».
- Evidence: logcat на SM-S938B, handoff M20.

### OI-M20-002 — Заглушка RecognitionService в метаданных VIS

- Status: OPEN
- Source session: M20
- Component: Android assistant / STT
- Question: платформа требует `recognitionService` в `voice-interaction-service`; сейчас `MiaRecognitionService` всегда отвечает `ERROR_CLIENT`. Нужно ли связать его с on-device STT M21?
- Why non-blocking: сервис без intent-filter, не предлагается системой как распознаватель; голос идёт по каскаду M22.
- Current assumption: заглушка остаётся до решения M21/M22.
- Options: оставить заглушку; реализовать поверх `SpeechRecognizer` из M21.
- Recommendation: оставить заглушку в MVP.
- Affects sessions: M21, M22.
- Revisit trigger/by: когда M22 подключит `SpeechRecognizer`.
- Resolution:
- Evidence:

## M21 inbox

### OI-M21-001 — Уверенность STT без оценок модели

- Status: OPEN
- Source session: M21
- Component: Android STT (`:core:stt`)
- Question: как получить калиброванную уверенность распознавания?
- Why non-blocking: `SttResult.Transcript.confidence` заполняется эвристикой (`TranscriptConfidence`, букв в секунду), помечена `ConfidenceSource.HEURISTIC`; годится как порог «повторите».
- Current assumption: sherpa-onnx 1.13.8 считает `ys_log_probs` только для NeMo transducer в C++, Kotlin/JNI их не отдаёт; у CTC их нет.
- Options: (a) оставить эвристику; (b) upstream-PR в sherpa-onnx (JNI `ysLogProbs`) и перейти на RNNT; (c) собственная JNI-сборка.
- Recommendation: (a) в MVP, (b) при первом реальном шуме ложных срабатываний.
- Affected future sessions: M22, M60
- Revisit trigger/deadline: device gate M22 на реальной речи.

### OI-M21-002 — Политика загрузки модели STT (~225 МБ)

- Status: DECIDED
- Source session: M21
- Component: Android STT / UX
- Question: когда и с чьего согласия качать модель: только Wi-Fi, явный запрос, фоновая загрузка (WorkManager)? Для релиза — Play Asset Delivery?
- Why non-blocking: `SttModelInstaller.install()` вызывает потребитель; до установки `readiness = ModelMissing`, голос может уйти в fallback.
- Current assumption: загрузка с HuggingFace (pinned commit + SHA-256) по явному действию пользователя, по умолчанию Wi-Fi.
- Options: (a) явная кнопка «Скачать распознавание (225 МБ)»; (b) авто на Wi-Fi + зарядке; (c) install-time asset pack в Play.
- Recommendation: (a) в MVP; (c) рассмотреть перед публикацией в Play (зеркало вместо HuggingFace).
- Affected future sessions: M22, M24, M62
- Revisit trigger/deadline: M22 (экран голоса) / до публичного релиза.
- Resolution: решение пользователя 2026-10-09 (MW1 integration): вариант (a) — Mia предлагает скачать модель распознавания по явному согласию пользователя, **в любой сети, Wi-Fi не требуется** (размер ~225 МБ показывается в предложении). Автозагрузка без согласия не делается. Play Asset Delivery / зеркало вместо HuggingFace — пересмотреть перед публикацией в Play.
- Evidence: решение пользователя в MW1 integration session; [M21](handoffs/M21.md).

### OI-M21-003 — Смешанная речь RU/EN и названия сервисов

- Status: OPEN
- Source session: M21
- Component: Android STT / run layer
- Question: как надёжно распознавать «Linear», «Slack», «Notion» и т. п.?
- Why non-blocking: имена людей распознаются хорошо (CTC 46/54 сущностей), а бренды CTC пишет кириллицей или теряет; punct-модели пишут латиницу, но хуже имена.
- Current assumption: дефолт CTC (кириллица); текст «гугл календарь», «телеграм» идёт в модель как есть — LLM обычно понимает.
- Options: (a) словарь алиасов (кириллица → бренд) в ядре/клиенте; (b) hotwords (только transducer + modified_beam_search); (c) RNNT-punct.
- Recommendation: (a) в M22/M13, замерить (b)/(c) на реальной речи.
- Affected future sessions: M22, M13, M41
- Revisit trigger/deadline: device gate M22.

### OI-M21-004 — WER на реальной речи

- Status: OPEN
- Source session: M21
- Component: Android STT / evaluation
- Question: каков WER на живых голосах (не синтез)?
- Why non-blocking: M21 замерил 30 фраз, синтезированных macOS `say` (Milena, чистый звук): WER 6.9 %, задержка ~0.1 с; этого достаточно для выбора CTC и бюджета задержки.
- Current assumption: на реальной речи WER выше; задержка не изменится.
- Options: записать те же 30 фраз (`core/stt/benchmark/phrases.json`) 3–5 голосами в шуме и прогнать `device_bench.sh`-схему с этими WAV.
- Recommendation: сделать в device gate M22.
- Affected future sessions: M22, M60
- Revisit trigger/deadline: M22.

### OI-M21-005 — ABI нативной части STT

- Status: OPEN
- Source session: M21
- Component: Android build
- Question: упаковывать ли armeabi-v7a/x86_64 (каждый ABI +20–30 МБ)?
- Why non-blocking: пакуется только arm64-v8a (`mia.stt.abis`); прочие ABI получают `NATIVE_RUNTIME_MISSING` и fallback.
- Current assumption: пилотные устройства arm64.
- Options: (a) только arm64; (b) ABI splits / App Bundle; (c) добавить x86_64 для эмулятора в debug.
- Recommendation: (a) в MVP, (b) при публикации AAB.
- Affected future sessions: M22, M62
- Revisit trigger/deadline: сборка релизного AAB.

## M13 inbox

- Нет записей.

## M14 inbox

### OI-M14-001 — Declarative servers expose an app-only `get_state` that the manifest cannot list

- Status: OPEN
- Source session: M14
- Component: Plugins / declarative host, manifest contract
- Question: the tile of a declarative plugin must read its state without an LLM, but the M00 rule `declarative actions == tools` (validator `check_manifest_semantics`) forbids declaring a read tool, and a tile `refresh.tool` must be in `tools`. How is the tile refreshed?
- Why non-blocking: the host serves an extra tool `get_state` marked `_meta.ui.visibility: ["app"]` (hidden from the model); the tile calls it once after the MCP App handshake and then receives pushed tool results. Builtin plugins are not affected (they declare a read tool).
- Current assumption: `get_state` is implicit for `server.kind: declarative`, absent from `tools`, never offered to models; declarative tiles have no `refresh` in the manifest.
- Options: (a) keep it implicit and let M13/M15 special-case `get_state` (policy: read, not voice, app-only; ignore it in the "tools/list equals manifest" publication check); (b) relax the validator/schema so a declarative manifest may declare `get_state` (and refresh with it); (c) add a `view.refreshSeconds` field.
- Recommendation: (a) for the MVP, then (b) additively in the contract when M15 writes the publication check.
- Affects sessions: M13, M15, M30, M31.
- Revisit trigger/by: M15 (publication checks) and M31 (dashboard refresh).
- Resolution:
- Evidence: `app/modules/plugins/declarative/host.py` (`GET_STATE_TOOL`), `tests/plugins/test_declarative.py::test_actions_become_tools_and_state_patch_persists_per_installation`.

### OI-M14-002 — No permission vocabulary for "any public HTTPS page"

- Status: OPEN
- Source session: M14
- Component: Plugins / manifest permissions
- Question: `builtin.internet.read(url)` fetches arbitrary public HTTPS pages, but `permissions.network` is an allow-list of hosts (SSRF list for remote servers).
- Why non-blocking: the manifest lists the fixed search hosts (`news.google.com`, `www.bing.com`); the client itself refuses non-public addresses (DNS pinning, all answers must be global, redirects re-validated, size/time bounds), so the host list is advisory for builtin plugins.
- Current assumption: builtin plugins are first-party, so the consent screen shows the listed hosts plus the SKILL.md description of `read`.
- Options: additive `permissions.network` wildcard token (e.g. `"public-web"`) rendered as "any public site"; a separate boolean `permissions.publicWeb`; keep as is.
- Recommendation: add `publicWeb: boolean` additively to the schema in M15 and use it in the consent screen.
- Affects sessions: M15, M30.
- Revisit trigger/by: M15 consent screen.
- Resolution:
- Evidence: `app/plugins/builtin/internet/mia.json`, `app/modules/plugins/builtin/web.py`.

### OI-M14-003 — Mia-clock plugin and v2 ids that cannot be converted

- Status: OPEN
- Source session: M14
- Component: Plugins / migration from the web POC
- Question: (1) `mia.clock` (world clock) is a POC builtin that is not in the M14 card and was not ported; (2) a POC v2 id such as `community.1app` (slug starting with a digit) does not match the `mia.json` id pattern, so the converter refuses it instead of renaming.
- Why non-blocking: the six card plugins are done; POC apps with a letter-first slug (all examples) convert; the converter reports the exact problem.
- Current assumption: clock is dropped or added later as a builtin; digit-first ids are rare and need a manual rename.
- Options: port `mia.clock` (now/add_zone/remove_zone, `zoneinfo`) as `builtin.clock`; auto-prefix digit-first slugs with `app-` and record the id mapping for M10's legacy import.
- Recommendation: decide with the M31 web cleanup; port the clock only if users of the POC use it.
- Affects sessions: M15, M30, M31.
- Revisit trigger/by: M31 (removal of `app/web_poc`).
- Resolution:
- Evidence: `app/modules/plugins/declarative/convert.py` (`_envelope`), `docs/implementation/handoffs/M14.md`.

### OI-M14-004 — Firing contract for stored deadlines (M16)

- Status: OPEN
- Source session: M14
- Component: Plugins / jobs
- Question: timer, alarm and focus only store `items[]` in `PluginState` (`deadline_ms`, `status`, `label`); who marks an item as fired and how is firing de-duplicated?
- Why non-blocking: M14 stores the deadline only (card); `deadlines.pending(items)` returns what still waits; expired items stay in state for 24 h (pruned on the next write) so a late scheduler can still deliver them.
- Current assumption: M16 keeps its own fired marker per `(installation_id, item id, deadline_ms)` in its job table and does not rewrite plugin state; paused timers are not due.
- Options: M16 writes `fired_ms` into the item; M16 creates a one-shot job from `start`/`set` (needs a tool-result hook); poll `PluginState` every 30 s.
- Recommendation: one-shot job created by the router/host when a tool result carries `structuredContent.changed` with a `deadline_ms`, falling back to the periodic scan.
- Affects sessions: M13, M16, M31.
- Revisit trigger/by: M16.
- Resolution:
- Evidence: `app/modules/plugins/builtin/deadlines.py`, `tests/plugins/test_countdown_plugins.py`.

### OI-M14-005 — Host LLM budget and scheduled digest runs

- Status: OPEN
- Source session: M14
- Component: Plugins / model access
- Question: `permissions.llm.viaHost` plugins (`builtin.news`, declarative `model.prompt`) call `HostServices.llm`; who enforces `monthlyBudgetUsd`, and which model/connection is used when a scheduled job refreshes the digest while the user is offline?
- Why non-blocking: the protocol (`HostLlm.generate(prompt, identity, plugin_id)`) is defined and a clear tool error ("Выберите модель…") is returned when none is wired or available; nothing is charged by M14 itself.
- Current assumption: the integrator implements `HostLlm` over M11's gateway with the user's primary model and a per-installation budget check.
- Options: budget counter in M11's usage ledger keyed by `installation_id`; allow jobs to run only with the primary connection; block scheduled refresh when the connection needs a user session.
- Recommendation: implement over the gateway in the MW2 integration and cap `refresh` frequency by `interval_minutes >= 30`.
- Affects sessions: M11, M15, M16.
- Revisit trigger/by: MW2 integration, M16.
- Resolution:
- Evidence: `app/modules/plugins/builtin/services.py` (`HostLlm`), `tests/plugins/test_notes_internet_news.py`.

### OI-M14-006 — MCP App bridge is verified at protocol level only

- Status: OPEN
- Source session: M14
- Component: Plugins / MCP Apps UI
- Question: tiles and the `window.mia` bridge follow the MCP Apps JSON-RPC over `postMessage` (`ui/initialize`, `tools/call`, `ui/notifications/tool-result`), but no real MCP Apps host exists yet in this repository.
- Why non-blocking: scripts are syntax-checked and the bridge protocol is exercised under node with a fake host; tiles are plain DOM code with `textContent` only and a restrictive CSP.
- Current assumption: M31's dashboard host implements the same three messages and forwards `tools/call` through the router with the installation identity.
- Options: adapt the bridge once the M31 host is written; adopt the official `@modelcontextprotocol/ext-apps` client in tiles.
- Recommendation: M31 runs each tile in its iframe sandbox and adjusts `app/modules/plugins/builtin/bridge.py` if the protocol version or message names differ.
- Affects sessions: M30, M31, M24.
- Revisit trigger/by: M31.
- Resolution:
- Evidence: `tests/plugins/test_bridge_js.py`, `app/modules/plugins/builtin/bridge.py`.

### OI-M14-007 — `mcp` pulls `httpx2`, which changes Starlette's TestClient

- Status: OPEN
- Source session: M14
- Component: Dependencies / tests
- Question: `mcp==2.2.0` depends on `httpx2`; once installed, Starlette's `TestClient` returns `httpx2` responses and cookies, which broke three `tests/hardening` assertions and three mypy lines in `tests/web_poc`.
- Why non-blocking: fixed in M14 with minimal test-only edits (`cast` and `assert ... is not None`); M13 pins the same `mcp` and will meet the same failures, so the integrator should expect an identical (or conflicting) edit.
- Current assumption: keep `httpx` for application code, accept `httpx2` as a transitive dependency.
- Options: migrate tests to a single client library; pin an older `mcp`; ignore.
- Recommendation: keep the edits, resolve the possible merge conflict by taking either identical hunk.
- Affects sessions: M13, MW2 integrator.
- Revisit trigger/by: MW2 integration.
- Resolution:
- Evidence: commit `af56a4d`, `tests/hardening/test_admission_and_resilience.py`, `tests/web_poc/test_accounts.py`.

## M22 inbox

- Нет записей.

## M15 inbox

- Нет записей.

## M16 inbox

- Нет записей.

## M17 inbox

- Нет записей.

## M23 inbox

- Нет записей.

## M30 inbox

- Нет записей.

## M40 inbox

- Нет записей.

## M41 inbox

- Нет записей.

## M42 inbox

- Нет записей.

## M50 inbox

- Нет записей.

## M24 inbox

- Нет записей.

## M31 inbox

- Нет записей.

## M43 inbox

- Нет записей.

## M51 inbox

- Нет записей.

## M60 inbox

- Нет записей.

## M61 inbox

- Нет записей.

## M62 inbox

- Нет записей.
