# MIA: план автономных implementation-сессий

Статус: рабочий orchestration contract  
Дата: 2026-09-10  
Основная архитектура: docs/POC_ARCHITECTURE_AND_IMPLEMENTATION_PLAN.md

## 1. Назначение

Этот документ разбивает реализацию PoC на автономные сессии с ограниченным контекстом. Каждая сессия имеет:

- один проверяемый результат;
- явные зависимости;
- ограниченный read/write scope;
- входной context packet;
- обязательные тесты и Definition of Done;
- собственный handoff;
- обязанность зарегистрировать неблокирующие вопросы в общем OPEN_ISSUES.md.

Цели разбиения:

1. Не передавать новой модели всю историю проекта.
2. Выполнять независимые backend и Android задачи параллельно.
3. Не допускать одновременного изменения одних и тех же файлов.
4. Завершать каждую сессию воспроизводимым состоянием.
5. Позволить заменить GPT на Claude или наоборот без изменения контракта сессии.

## 2. Coordination files

Источник истины находится в backend/coordination repository:

~~~text
/Users/aleksey/PycharmProjects/mia-helper
~~~

Обязательные файлы:

~~~text
AGENTS.md
CLAUDE.md
session_plan.md
docs/implementation/SESSION_EXECUTION_PLAN.md
docs/implementation/SESSION_PROMPT_TEMPLATE.md
docs/implementation/STATUS.md
docs/implementation/OPEN_ISSUES.md
docs/implementation/DEVICE_GATE.md
docs/implementation/handoffs/_TEMPLATE.md
docs/implementation/handoffs/Sxx.md
~~~

Android-код будет находиться в:

~~~text
/Users/aleksey/PycharmProjects/mia-helper-android
~~~

Android-сессия может создать code commit в Android repository и coordination commit с handoff/open issues в mia-helper. Оба commit ID указываются в handoff, но исполнитель извлекает их сам: пользователь не передаёт SHA вручную.

## 3. Неподвижный контракт сессии

### 3.0 Короткий пользовательский вызов

Полным запросом на реализацию считается одна строка:

~~~text
Выполни сессию S10 из session_plan.
~~~

Также допустимо `Выполни S10`. Пользователь сообщает намерение и session ID; вся механическая оркестрация принадлежит исполнителю. Нельзя требовать от пользователя длинный prompt, backend/Android commit, branch, worktree, repository path, dependency handoffs, open issue IDs или test commands, если эти данные можно определить из coordination files и локального состояния.

После короткой команды dispatcher:

1. Находит session card и модель в этом документе.
2. Проверяет STATUS, прямые handoffs, git history/status/worktrees и write-scope ownership.
3. Сам определяет repository paths и base/head commits; до инициализации git использует `N/A`.
4. Формирует внутренний context packet по `SESSION_PROMPT_TEMPLATE.md`.
5. Если зависимости готовы, выполняет session целиком, тестирует, создаёт handoff и обновляет coordination files.
6. Если зависимость не готова, останавливается до изменения кода, называет точную причину и предлагает короткую следующую команду. Он не выполняет prerequisite молча.

Пользовательский вопрос допустим только для настоящего blocker: продуктового выбора, нового разрешения, недоступного external state или потенциально разрушительного действия. Отсутствие вручную указанного SHA не является blocker.

### 3.1 Вход

Dispatcher автоматически собирает для каждой сессии только:

1. Этот документ:
   - разделы 3–9;
   - собственную session card;
   - не все остальные session cards.
2. Собственную строку из STATUS.md.
3. Handoff только прямых зависимостей.
4. Релевантные активные записи OPEN_ISSUES.md по session/component.
5. Указанные в session card разделы архитектурного документа.
6. Указанные source files и contracts.
7. Текущие git status/log/diff/worktree данные для затрагиваемых repositories.

InitialPlan.md читается только S00 либо сессией, которая явно получила задачу пересмотреть продуктовый scope. Обычная implementation-сессия его не загружает.

`SESSION_PROMPT_TEMPLATE.md` является внутренним assembly template для агента, а не формой для пользователя.

Содержимое .env не входит ни в один context packet. Сессия может проверить только наличие обязательных переменных безопасным config validation или запустить явно разрешённый smoke test. Значения секретов не выводятся.

### 3.2 Максимальный initial packet

Цель — не более 8–12 тысяч input tokens до начала исследования кода:

- session prompt: до 1 500 tokens;
- собственная session card: до 1 500;
- handoffs: до 1 500 на файл, обычно один или два;
- relevant architecture excerpt: до 4 000;
- open issues summary: до 1 000;
- остальное — резерв.

Исходный код читается по необходимости через поиск и точечные диапазоны. Нельзя заранее загружать весь repository, весь architecture document или все handoffs.

### 3.3 Обязательный output

Сессия завершает работу только после создания:

1. Реализации в пределах write scope.
2. Тестов в пределах scope.
3. Выполненных verification commands.
4. Одного или нескольких небольших, логичных commits, если repository уже использует git.
5. Файла docs/implementation/handoffs/Sxx.md по шаблону.
6. Обновления своего блока в OPEN_ISSUES.md:
   - новый вопрос;
   - уточнение;
   - либо явная запись, что новых вопросов нет, только в handoff.
7. Короткого финального сообщения пользователю со ссылкой на handoff.

### 3.4 Запрещённый output

- unrelated refactoring;
- изменение чужого session scope без фиксации зависимости;
- незадокументированные TODO/FIXME;
- копирование длинных логов или diff в handoff;
- реальные secrets;
- статус completed при непрошедшем обязательном тесте;
- решение продуктового вопроса без записи rationale, если он уже находится в OPEN_ISSUES.

### 3.5 Session status

Допустимы:

- COMPLETED — весь Definition of Done выполнен;
- PARTIAL — безопасная часть готова, оставшаяся работа точно описана;
- BLOCKED — результат нельзя завершить без решения/authority/external state;
- FAILED — изменения не должны быть интегрированы.

OPEN_ISSUES.md содержит только вопросы, которые не блокируют текущую session. Настоящий blocker помещается в handoff и STATUS.md. Его нельзя маскировать как open issue.

## 4. Handoff contract

Handoff должен позволить следующей модели начать без чтения предыдущего чата.

Максимальный размер: 200 строк или примерно 1 500 tokens. Если требуется больше, подробность должна находиться в code/docs/tests, а handoff содержит ссылки.

Обязательные поля:

- session ID, status, model/effort;
- repositories и base/head commits;
- выполненный scope;
- фактические изменения;
- public contracts/API/schema, появившиеся в сессии;
- migrations/config variables;
- verification commands и результаты;
- незавершённая работа;
- blockers;
- OPEN_ISSUES IDs;
- точный input для следующих sessions;
- known-good starting command.

Следующая session доверяет handoff только вместе с проверкой git status, commit и запуском указанного smoke test.

## 5. OPEN_ISSUES contract

OPEN_ISSUES.md — не backlog задач, а журнал не блокирующих решений и вопросов.

Примеры:

- минимальный Android API ещё не выбран, но scaffold может использовать временное значение;
- нужно измерить два значения VAD threshold, но текущая реализация имеет безопасный default;
- точный retention transcript будет решён до beta, а внутренний PoC может продолжаться.

Не относятся к open issues:

- тесты красные;
- обязательный credential отсутствует для session, которая должна выполнить real smoke;
- schema несовместима с прямой зависимостью;
- неизвестный выбор меняет весь публичный API и без него нельзя писать код.

Каждая запись имеет:

- ID вида OI-S16-001;
- status: OPEN, DECIDED, RESOLVED или SUPERSEDED;
- source session;
- component;
- question;
- current assumption;
- options;
- recommendation;
- affected future sessions;
- revisit trigger/deadline;
- resolution и evidence, когда вопрос закрыт.

Правила:

1. Session создаёт и изменяет записи только в своём заранее выделенном блоке.
2. Если последующая session решает чужой вопрос, она добавляет resolution update в свой блок и ссылается на исходный ID.
3. Integration session переносит итоговое решение в исходную запись или ADR.
4. Нельзя удалять записи; разрешено менять status и добавлять resolution.
5. Вопрос, ставший blocker, переносится в handoff/STATUS, а запись получает пометку ESCALATED.

Такая структура уменьшает merge conflicts между параллельными branches.

## 6. Git и параллельная работа

### 6.1 Branch/worktree

После S00 каждая session работает в отдельной branch/worktree:

~~~text
session/S10-backend-bootstrap
session/S20-android-bootstrap
session/S16-gemini-live
~~~

Параллельные sessions не должны работать в одном checkout.

### 6.2 Ownership

Session владеет только путями из своей card. Общие root files:

- Gradle version catalog/settings;
- pyproject/lockfile;
- Docker Compose;
- OpenAPI/realtime schemas;
- shared domain models;
- database migration chain;

редактируются только назначенным owner либо последующей integration session.

Если параллельная session требует shared-file change:

1. Она не делает скрытое изменение.
2. Регистрирует точный requested diff в handoff/open issue.
3. Владелец или wave integrator применяет изменение.

### 6.3 Merge

После каждой wave один integration pass:

1. проверяет handoffs;
2. сливает branches в dependency order;
3. разрешает только ожидаемые conflicts;
4. запускает общий gate;
5. обновляет STATUS.md;
6. консолидирует OPEN_ISSUES;
7. фиксирует integration commit.

Если conflict показывает нарушение ownership, merge не продолжается автоматически.

### 6.4 Максимальная параллельность

Рекомендуется не более четырёх активных implementation-сессий:

- это соответствует четырём независимым tracks;
- сохраняет управляемый merge surface;
- не ускоряет проект за счёт одновременной правки root files;
- позволяет одной integration session быстро проверять wave.

## 7. Model policy

### 7.1 GPT/Codex

Основной выбор:

| Модель | Использование |
| --- | --- |
| GPT-5.6 Sol high | default для изолированной production implementation, tests и обычной интеграции |
| GPT-5.6 Sol xhigh | contracts, concurrency, realtime, audio, security boundaries, сложная диагностика и финальный audit |
| GPT-5.6 Terra high | scaffold, обычный UI и механическая реализация с уже стабильным контрактом |
| GPT-5.6 Luna medium/high | только форматирование, fixtures, простые docs/lint follow-ups; не владелец core session |

High является default. Xhigh выбирается, когда ошибка проявится между несколькими подсистемами или потребует трудного reasoning. Большое context window не является разрешением загружать лишние файлы.

### 7.2 Claude

Claude Max подписка считается доступным способом выполнить отдельную session, но не причиной запускать Claude вторым исполнителем поверх уже завершённой GPT-сессии.

| Модель | Использование |
| --- | --- |
| Claude Fable 5.1 high | critical architecture/contracts и long-horizon work, где важнее надёжность сквозного reasoning, чем минимальная стоимость |
| Claude Fable 5.1 xhigh | capability-sensitive realtime/concurrency/security integration и финальный release audit |
| Claude Opus 5 high/xhigh | сложный repair или substitute, когда Fable недоступен в конкретном runtime |
| Claude Sonnet 5 high | bounded feature implementation, UI, tests, adapters со стабильным контрактом |
| Claude Haiku 4.5 | механические follow-ups и triage; не основной исполнитель core session |

Fable 5.1 назначен шести наиболее ответственным core sessions: S00, S16, S17, S26, S31 и S33. Для S00 достаточно `high`; для остальных выбран `xhigh`. Effort `max` намеренно не является default: наличие Claude Max подписки описывает доступ, а не необходимость максимального расхода reasoning tokens.

### 7.3 Как не удваивать расход

- В таблице ниже одна модель является preferred executor, вторая — fallback, а не второй обязательный исполнитель.
- Нельзя запускать обе модели на одну implementation session только «для уверенности».
- Cross-model review применяется только к S16, S17, S26, S31 или S33 и только если конкретный риск оправдывает стоимость.
- Reviewer получает commit diff, tests и handoff, а не весь исходный context packet.
- Модель не повышается до xhigh для lint, formatting, boilerplate или исправления одного локального теста.
- После двух неудачных существенно разных подходов session закрывается PARTIAL/BLOCKED; создаётся новая repair session с чистым контекстом.

### 7.4 Context envelope

Значение в session table — верхняя граница осмысленно загруженного source/docs контекста, не цель расхода и не общий лимит скрытых reasoning tokens.

- 20K: один модуль, стабильный контракт;
- 30–35K: feature с DB/API/client boundary;
- 40–45K: provider/realtime/audio/security;
- 60–65K: end-to-end integration/audit нескольких handoffs.

При достижении envelope session должна:

1. закончить текущую безопасную единицу;
2. выполнить tests;
3. написать PARTIAL handoff;
4. предложить узкую follow-up session.

Она не продолжает расширять контекст до полного repository.

## 8. Parallel execution graph

~~~text
W0:
  S00
   │
   ├───────────────┐
W1:│               │
  S10             S20
   │               │
   ├────────┐      ├────────┐
W2:│        │      │        │
  S11      S13    S15      S24
   │        │      │        │
   ├──┐  ┌──┘      ├──┐  ┌──┘
W3:  S12  S14      S16  S25
        │    │        │    │
W4:    S17  S21      S22  S26
        │      \      /      │
W5:    S18      S23         S27
          \      │          /
W6:       S30   S32   S31   (S28 deferred)
             \   │   │
W7:              S33
~~~

Граф показывает wave placement, но scheduler может запускать session сразу после готовности всех её dependencies и освобождения slot. Не нужно ждать завершения всей wave.

Критические пути:

- voice backend: S00 → S10 → S15 → S16 → S31 → S33;
- voice Android: S00 → S20 → S24 → S25 → S26 → S31 → S33 (S27/S28 отложены, ADR-009);
- text/auth: S10/S20 → S11/S13/S12/S14/S21/S22/S23 → S30;
- tools: S13/S14/S15 → S17 → S32.

## 9. Session/model summary

| ID | Результат | Depends on | Wave | Preferred executor | Fallback | Context ceiling |
| --- | --- | --- | --- | --- | --- | --- |
| S00 | contracts и governance baseline | — | W0 | Fable 5.1 high | Sol xhigh | 35K |
| S10 | backend scaffold/CI/local infra | S00 | W1 | Terra high | Sonnet 5 high | 20K |
| S20 | Android scaffold/CI/modules | S00 | W1 | Terra high | Sonnet 5 high | 20K |
| S11 | DB и conversation persistence | S10 | W2 | Sol high | Sonnet 5 high | 30K |
| S13 | text provider и orchestration | S10, S00 | W2 | Sol xhigh | Opus 5 high | 35K |
| S15 | realtime gateway и fake provider | S10, S00 | W2 | Sol xhigh | Opus 5 high | 40K |
| S24 | Android audio primitives | S20 | W2 | Sol xhigh | Opus 5 high | 35K |
| S12 | Google auth backend | S10, S11 | W3 | Sol high | Sonnet 5 high | 30K |
| S14 | conversation REST и text SSE | S11, S13 | W3 | Sol high | Sonnet 5 high | 30K |
| S16 | Gemini Live adapter | S13, S15 | W3 | Fable 5.1 xhigh | Sol xhigh | 45K |
| S25 | Android realtime/voice controller | S15, S20, S24 | W3 | Sol xhigh | Opus 5 high | 40K |
| S17 | tool registry/policy/executor | S13, S14, S15 | W4 | Fable 5.1 xhigh | Sol xhigh | 40K |
| S21 | Android Google auth/session | S12, S20 | W4 | Sol high | Sonnet 5 high | 25K |
| S22 | Android history/cache/sync | S11, S14, S20 | W4 | Sol high | Sonnet 5 high | 30K |
| S26 | Android VAD/barge-in | S16, S24, S25 | W4 | Fable 5.1 xhigh | Sol xhigh | 45K |
| S18 | backend hardening/deployment | S12, S14, S16, S17 | W5 | Sol high | Opus 5 high | 45K |
| S23 | Android text chat UI/SSE | S14, S20, S22 | W5 | Terra high | Sonnet 5 high | 30K |
| S27 | foreground wake phrase (DEFERRED, ADR-009) | S24, S26 | W5 | Sol xhigh | Opus 5 high | 40K |
| S28 | ROLE_ASSISTANT/system assistant spike (DEFERRED, ADR-009) | S27 | W6 | Sol xhigh | Fable 5.1 high | 45K |
| S30 | auth + text E2E integration | S12, S14, S21, S23 | W6 | Sol high | Opus 5 high | 35K |
| S31 | voice E2E integration | S16, S25, S26 | W6 | Fable 5.1 xhigh | Sol xhigh | 60K |
| S32 | tool E2E integration | S17, S23, S25 | W6 | Sol high | Opus 5 high | 40K |
| S33 | final beta audit/release gate | S18, S30, S31, S32 | W7 | Fable 5.1 xhigh | Sol xhigh | 65K |

Из 23 core sessions:

- 14 выполняются преимущественно на Sol high/xhigh;
- 3 scaffold/UI sessions — на Terra high;
- 6 наиболее ответственных — на Fable 5.1 high/xhigh;
- Luna/Haiku оставлены только для мелких follow-ups;
- fallback запускается только при недоступности preferred model или после зафиксированного неуспеха, а не параллельно.

## 10. Session cards

### S00 — Contracts and governance baseline

**Objective:** превратить архитектуру в неизменяемый baseline, от которого независимо стартуют backend и Android.

**Inputs:**

- InitialPlan.md;
- architecture sections 1–10, 17–21;
- текущие docs;
- текущий filesystem/git status.

**Write scope:**

- docs/implementation/**;
- docs/adr/**;
- contracts/**;
- repository root только для git/.gitignore/README governance.

**Work:**

- инициализировать git, если это ещё не сделано;
- зафиксировать package/application ID assumption;
- создать REST/OpenAPI skeleton;
- создать realtime v1 JSON Schema/event fixtures;
- зафиксировать normalized AIEvent и error codes;
- определить commit/branch/test naming;
- убедиться, что .env исключён из git без чтения содержимого.

**Outputs:**

- contracts baseline;
- ADR-001…ADR-007 либо ссылки на архитектурные ADR;
- S00 handoff;
- STATUS/open issues update.

**Verification/DoD:**

- schemas синтаксически валидны;
- event examples проходят schema validation;
- нет secret files в git index;
- S10 и S20 могут начать только по S00 handoff.

**Consumers:** S10, S20, S13, S15.

### S10 — Backend scaffold

**Objective:** создать запускаемый backend skeleton и local infrastructure.

**Inputs:**

- S00 handoff;
- architecture 7.1–7.2, 15.1;
- contracts baseline.

**Write scope:** backend root, app/core, app/main, tests bootstrap, docker-compose, pyproject/lockfile, CI.

**Work:**

- FastAPI/config/logging/database/Redis skeleton;
- health live/ready;
- fake settings;
- Docker Compose;
- Ruff/typecheck/pytest;
- documented commands.

**Outputs:** runnable backend, S10 handoff.

**Verification/DoD:** clean install, lint, typecheck, tests, health checks.

**Consumers:** S11, S12, S13, S15.

### S20 — Android scaffold

**Objective:** создать отдельный Android repository с устойчивыми module boundaries.

**Inputs:**

- S00 handoff;
- architecture 4, 11.1–11.4;
- REST/realtime contracts.

**Write scope:** весь новый Android repository; coordination docs only for handoff/issues.

**Work:**

- Gradle/Compose/version catalog;
- app/core/feature module skeleton;
- build variants;
- DI/navigation/theme;
- unit/lint/assemble CI;
- fake repositories.
- компактные Android-root `AGENTS.md` и `CLAUDE.md`, направляющие session orchestration к coordination repository без дублирования полного плана.

**Outputs:** compiling Android project, S20 handoff with Android commit.

**Verification/DoD:** assembleDebug, unit tests, lint.

**Consumers:** S21, S22, S23, S24, S25.

### S11 — Persistence and conversation domain

**Objective:** реализовать canonical user/conversation/turn/entry persistence.

**Inputs:**

- S10 handoff;
- architecture 7.5, 10;
- S00 contract types.

**Write scope:** app/modules/users, conversations repositories/models/services, migrations, repository tests.

**Work:**

- SQLAlchemy models;
- Alembic initial migration;
- repositories с ownership filtering;
- sequence/idempotency;
- transaction boundaries;
- cursor primitives.

**Outputs:** schema and domain services, S11 handoff.

**Verification/DoD:** migration up/down/up, PostgreSQL integration tests, ownership/idempotency tests.

**Consumers:** S12, S14, S22.

### S13 — Text provider and orchestration

**Objective:** реализовать provider-neutral text streaming и Gemini text adapter.

**Inputs:**

- S00 and S10 handoffs;
- architecture 7.3–7.5;
- normalized AIEvent contract;
- official Gemini text/function calling docs.

**Write scope:** app/providers/ai/base/fake/gemini_text, app/modules/orchestration, provider tests.

**Work:**

- TextAIProvider;
- FakeAIProvider;
- Gemini mapping;
- ContextBuilder/token budget;
- manual function-call event contract без исполнения tools;
- provider error mapping/usage.

**Outputs:** tested stream abstraction, S13 handoff.

**Verification/DoD:** deterministic fake tests; Gemini smoke при наличии credential; absence of credential skips only external smoke.

**Consumers:** S14, S16, S17.

### S15 — Realtime gateway and fake provider

**Objective:** реализовать MIA realtime state machine до подключения Gemini.

**Inputs:**

- S00 and S10 handoffs;
- architecture 6.3–6.4, 8;
- realtime schemas/fixtures.

**Write scope:** app/modules/realtime, fake realtime provider, WS contract/integration tests.

**Work:**

- session creation;
- WSS auth hook;
- text control/binary PCM routing;
- bounded queues;
- sequence/response filtering;
- cancellation/disconnect;
- deterministic fake audio/transcript/tool events.

**Outputs:** provider-neutral realtime gateway, S15 handoff.

**Verification/DoD:** handshake/schema/backpressure/cancel/disconnect integration tests.

**Consumers:** S16, S17, S25.

### S24 — Android audio primitives

**Objective:** доказать стабильный capture/playback/full-duplex pipeline без сети.

**Inputs:**

- S20 handoff;
- architecture 11.4–11.6;
- audio formats from realtime contract.

**Write scope:** :core:audio and its tests/debug harness.

**Work:**

- AudioRecord PCM16 mono 16 kHz;
- AudioTrack PCM16 mono 24 kHz;
- bounded buffers;
- focus/attributes;
- cancellation/flush;
- loopback or deterministic generated audio test;
- capability diagnostics.

**Outputs:** reusable audio engine, S24 handoff.

**Verification/DoD:** physical-device capture/playback; resource leak loop; no UI/network dependency.

**Consumers:** S25, S26, S27.

### S12 — Backend Google auth

**Objective:** реализовать Google ID-token exchange и собственные MIA sessions.

**Inputs:**

- S10/S11 handoffs;
- architecture 6.1, 9 auth endpoints, 13.1–13.2;
- auth contract;
- official Google token verification docs.

**Write scope:** app/modules/auth, related migration/repository/tests, auth API.

**Work:**

- nonce with TTL;
- signature/aud/iss/exp/nonce verification;
- upsert by Google sub;
- short access JWT;
- rotating hashed refresh tokens/reuse detection;
- logout/me;
- rate limits.

**Outputs:** backend auth flow, S12 handoff.

**Verification/DoD:** invalid claims/nonce/replay/rotation tests; no secret logging.

**Consumers:** S21, S30, S18.

### S14 — Conversation REST and text SSE

**Objective:** предоставить стабильный text chat/history API.

**Inputs:**

- S11/S13 handoffs;
- architecture 6.2, 9;
- REST contract.

**Write scope:** conversation API schemas/routes, SSE service, API integration tests.

**Work:**

- CRUD/list/entries;
- cursor pagination;
- message:stream;
- pending/completed/failed lifecycle;
- idempotency/retry;
- stream cancellation.

**Outputs:** versioned REST/SSE API, updated contract only through compatible changes, S14 handoff.

**Verification/DoD:** fake-provider E2E, auth ownership, duplicate retry and disconnect tests.

**Consumers:** S17, S22, S23, S30.

### S16 — Gemini Live adapter

**Objective:** подключить Gemini Live, сохранив внутренний realtime contract.

**Inputs:**

- S13/S15 handoffs;
- architecture 7.4, 8;
- official Gemini Live capabilities/session/tool docs;
- realtime provider port.

**Write scope:** app/providers/ai/gemini_live, mapping tests, provider smoke tests; no changes to public WS schema без explicit ADR.

**Work:**

- provider connection/config;
- PCM input/output mapping;
- input/output transcript events;
- tool call/cancel mapping;
- interrupted/session resumption/GoAway;
- timeouts and usage;
- sanitized errors;
- mock transcript/audio fixtures.

**Outputs:** GeminiLiveAdapter, S16 handoff.

**Verification/DoD:** unit mapping suite, fake transport tests, real smoke with configured GEMINI_API_KEY; secret never printed.

**Consumers:** S18, S26, S31.

### S25 — Android realtime and voice controller

**Objective:** связать Android audio engine с MIA WebSocket через детерминированную state machine.

**Inputs:**

- S15/S20/S24 handoffs;
- architecture 8, 11.4;
- realtime fixtures.

**Write scope:** :core:network realtime, :feature:voice controller/state machine, tests; минимальный debug UI.

**Work:**

- session REST + authenticated WSS;
- control/binary frames;
- sequence/response IDs;
- capture → socket → playback;
- reconnect/error states;
- stale audio filtering;
- protocol fixture tests.

**Outputs:** end-to-end client against fake gateway, S25 handoff.

**Verification/DoD:** reducer tests, fake WS integration, physical-device voice loop.

**Consumers:** S26, S31, S32.

### S17 — Tool registry, policy and executor

**Objective:** безопасно выполнить первый read-only tool в text и voice.

**Inputs:**

- S13/S14/S15 handoffs;
- architecture 7.6, 13.3;
- function-call contracts.

**Write scope:** app/modules/tools, tool execution persistence/migration, orchestration/realtime adapters, tests.

**Work:**

- typed ToolDefinition/Registry;
- JSON/Pydantic validation;
- manual loop;
- policy/risk/permissions;
- timeout/output limit;
- audit/idempotency;
- system.get_current_datetime;
- confirmation interface без write-tool.

**Outputs:** secure read-only tool path, S17 handoff.

**Verification/DoD:** invalid/unknown/oversized/timeout/cancel tests, prompt-injection result test.

**Consumers:** S18, S32.

### S21 — Android Google auth/session

**Objective:** реализовать clean-install sign-in, refresh и logout.

**Inputs:**

- S12/S20 handoffs;
- architecture 6.1, 11.1, 13.1–13.2;
- auth API fixtures.

**Write scope:** :core:auth, :feature:auth, secure token storage, auth tests.

**Work:**

- Credential Manager;
- authorized accounts/fallback;
- nonce and ID-token exchange;
- access refresh;
- Keystore-backed storage;
- logout/session restore.

**Outputs:** authenticated Android app, S21 handoff.

**Verification/DoD:** fake API tests, real Google sign-in on debug signing key, expiry/refresh/logout tests.

**Consumers:** S30.

### S22 — Android history/cache/sync

**Objective:** реализовать offline-friendly conversation list/history cache.

**Inputs:**

- S11/S14/S20 handoffs;
- architecture 9–10, 11.7;
- REST fixtures.

**Write scope:** :core:database, conversation repository, list feature data layer/tests.

**Work:**

- Room entities/migrations;
- REST pagination;
- server sequence merge;
- optimistic pending entries;
- idempotent retry;
- process restart sync.

**Outputs:** cached conversation repository, S22 handoff.

**Verification/DoD:** pagination/merge/restart/duplicate tests.

**Consumers:** S23.

### S26 — Android VAD and barge-in

**Objective:** обеспечить low-latency end-of-speech и гарантированную отмену устаревшего playback.

**Inputs:**

- S16/S24/S25 handoffs;
- architecture 6.4, 8.5, 11.4–11.6;
- interruption fixtures.

**Write scope:** :core:audio VAD/duplex, voice controller cancellation, tests/debug metrics.

**Work:**

- local VAD;
- hybrid audio commit;
- speech-during-playback detection;
- AudioTrack immediate flush;
- response.cancel;
- old response discard;
- latency instrumentation.

**Outputs:** measured barge-in, S26 handoff.

**Verification/DoD:** deterministic VAD corpus, interruption protocol test, physical device target below 200 ms where supported.

**Consumers:** S27, S31.

### S18 — Backend hardening and deployment

**Objective:** подготовить backend к internal beta, не меняя feature scope.

**Inputs:**

- S12/S14/S16/S17 handoffs;
- architecture 13–15;
- active backend open issues.

**Write scope:** core security/telemetry, deployment config, ops docs, cross-module backend tests.

**Work:**

- structured logs/redaction;
- correlation IDs/metrics;
- quotas/session limits/circuit breaker;
- readiness;
- migration release step;
- staging config;
- backup/restore procedure;
- dependency/security scan.

**Outputs:** deployable beta backend, S18 handoff.

**Verification/DoD:** full backend gate, container smoke, migration on empty/existing DB, no secret/content logging.

**Consumers:** S33.

### S23 — Android text chat UI/SSE

**Objective:** завершить пользовательский text chat поверх готового repository/API.

**Inputs:**

- S14/S20/S22 handoffs;
- architecture 11.8;
- SSE fixtures/design assumptions.

**Write scope:** :feature:conversation-list, :feature:chat, design components strictly needed by them.

**Work:**

- list/chat screens;
- streaming delta rendering;
- optimistic send/retry;
- tool/interrupted/failed presentation placeholders;
- accessibility/basic UI tests.

**Outputs:** usable text chat, S23 handoff.

**Verification/DoD:** Compose/unit tests, process restart, slow/error stream scenarios.

**Consumers:** S30, S32.

### S27 — Foreground wake phrase

**Status:** DEFERRED out of PoC scope by ADR-009. Код смержен и выключен по
умолчанию; корпус, latency, CPU и battery не измеряются, пока activation не
вернётся в scope.

**Objective:** добавить локальный wake-word trigger без отправки pre-trigger audio в облако.

**Inputs:**

- S24/S26 handoffs;
- architecture 12;
- selected temporary phrase/engine assumption from OPEN_ISSUES.

**Write scope:** wake-word engine module, foreground integration, evaluation harness.

**Work:**

- WakeWordEngine port;
- chosen KWS adapter;
- pre-roll;
- enable/disable UI setting;
- false accept/reject measurement;
- CPU/battery metrics.

**Outputs:** foreground wake activation, evaluation report, S27 handoff.

**Verification/DoD:** local-only pre-trigger path, measured corpus, no background promise.

**Consumers:** S28, S33.

### S28 — System assistant spike

**Status:** DEFERRED out of PoC scope by ADR-009; не начинается и не входит в
S33 gate.

**Objective:** определить реально поддерживаемый системный assistant/hotword режим на Android.

**Inputs:**

- S27 handoff;
- architecture 11.9, 12;
- official Android VoiceInteractionService/SoundTrigger docs;
- OEM test matrix.

**Write scope:** :feature:assistant/system service module, manifests/XML, spike tests/docs.

**Work:**

- ROLE_ASSISTANT onboarding;
- VoiceInteractionService/SessionService/Session;
- assist gesture;
- keyguard capability where permitted;
- AlwaysOnHotwordDetector availability;
- foreground/CPU fallback evaluation;
- OEM behavior report.

**Outputs:** working supported path plus explicit capability matrix, S28 handoff.

**Verification/DoD:** at least Pixel/AOSP and Samsung physical-device evidence; no unsupported claim.

**Consumers:** S33.

### S30 — Auth and text E2E integration

**Objective:** доказать clean install → sign-in → persistent streaming text conversation.

**Inputs:**

- S12/S14/S21/S23 handoffs;
- only relevant open issues;
- current integrated commits.

**Write scope:** auth/chat integration tests and minimal fixes inside those modules.

**Work:**

- real/debug sign-in;
- access expiry/refresh;
- text stream;
- process restart/history;
- duplicate retry;
- logout;
- compatibility check.

**Outputs:** E2E report/tests, minimal fixes, S30 handoff.

**Verification/DoD:** happy path and defined failure paths pass on clean environment/device.

**Consumers:** S33.

### S31 — Voice E2E integration

**Objective:** доказать Android microphone → backend → Gemini Live → Android playback и barge-in.

**Inputs:**

- S16/S25/S26 handoffs;
- current realtime schema;
- voice-related open issues;
- integrated backend/Android commits.

**Write scope:** voice/realtime integration tests, metrics, minimal fixes within those modules.

**Work:**

- tap-to-talk;
- transcripts/history;
- 20-turn soak;
- interruption;
- network disconnect/reconnect;
- latency measurement;
- Bluetooth/device cases as available.

**Outputs:** latency/soak report, fixes, S31 handoff.

**Verification/DoD:** real Gemini smoke; no stale audio after interruption; completed turns persist.

**Consumers:** S33.

### S32 — Tool E2E integration

**Objective:** доказать один tool через text и voice с audit trail.

**Inputs:**

- S17/S23/S25 handoffs;
- tool schemas;
- integrated commits.

**Write scope:** tool UI/events/integration tests and minimal fixes.

**Work:**

- selection/args/result;
- progress UI;
- invalid/unknown/timeout;
- audit/history;
- text and voice consistency.

**Outputs:** tool E2E report, fixes, S32 handoff.

**Verification/DoD:** deterministic fake test and Gemini smoke; model не обходит registry/policy.

**Consumers:** S33.

### S33 — Final beta audit and release gate

**Objective:** собрать waves, закрыть integration drift и выдать решение READY/NOT READY.

**Inputs:**

- S18/S30/S31/S32 handoffs;
- STATUS;
- active OPEN_ISSUES;
- git logs/diffs since S00;
- architecture acceptance criteria.

**Write scope:** cross-repo integration fixes, release docs, STATUS/OPEN_ISSUES/ADRs.

**Work:**

- verify all handoff claims;
- full clean backend/Android builds;
- migrations;
- fake E2E and real Gemini smoke;
- security/privacy/secret scan;
- latency/cost/error review;
- decide/defer open issues;
- publish known limitations and next phase.

**Outputs:** final audit, release checklist, S33 handoff, STATUS complete or blocked.

**Verification/DoD:** every architecture PoC criterion has evidence or explicit NOT READY finding.

**Consumers:** internal beta and next planning cycle.

## 11. Wave gates

### W0 gate

- contracts validate;
- ownership paths fixed;
- .env excluded;
- S10/S20 prompts can be generated without InitialPlan.

### W1 gate

- backend and Android compile independently;
- shared contract fixtures consumable from both;
- CI commands stable.

### W2 gate

- persistence, provider ports, realtime fake and audio core individually pass;
- no competing public contract definitions appeared.

### W3 gate

- auth/text/realtime client and real Live adapter exist;
- fake paths remain available without cloud credentials.

### W4 gate

- tool policy, Android auth/cache and barge-in pass component tests;
- dangerous writes still disabled.

### W5 gate

- backend deployable;
- Android text feature-complete in its scope;
- foreground wake merged but out of PoC scope (ADR-009): no quality, latency or
  battery claim, disabled by default.

### W6 gate

- three integration handoffs exist (S30/S31/S32; S28 is out of scope per
  ADR-009);
- capability/latency/limitations measured;
- no unresolved blocker hidden in OPEN_ISSUES.

### W7 gate

- clean release gate;
- known limitations and next-phase scope published;
- READY/NOT READY explicit.

## 12. Repair sessions

Repair session ID:

~~~text
S16-R1
S31-R2
~~~

Она создаётся, если:

- исходная session PARTIAL/BLOCKED;
- merge обнаружил regression;
- integration session нашла дефект за пределами своего safe fix scope.

Repair input:

- исходный handoff;
- failing command и минимальный log excerpt;
- regression commit/diff;
- релевантный contract;
- не весь первоначальный context.

Model escalation:

- Terra high → Sol high;
- Sol high → Sol xhigh;
- Sonnet 5 high → Opus 5 high;
- Sol/Opus xhigh → Fable 5.1 high/xhigh только для сложной long-horizon repair session;
- Fable 5.1 xhigh failure → новая архитектурная decision session, а не бесконечное продолжение.

Repair session создаёт отдельный handoff и не переписывает исходный.

## 13. Session start checklist

- Пользовательского session ID достаточно; внутренний packet собран dispatcher-ом без ручного ввода SHA/paths.
- STATUS показывает READY или все dependencies COMPLETED.
- Прямые handoffs существуют и commits доступны.
- Working trees clean.
- Branch/worktree уникальны.
- Write scope не пересекается с активной параллельной session.
- Нужные credentials существуют, если real smoke входит в DoD.
- .env не открывается и не выводится.
- Внутренний context packet содержит конкретные tests и stop conditions.

## 14. Session close checklist

- Scope реализован либо честно PARTIAL/BLOCKED.
- Targeted tests прошли.
- Full relevant gate прошёл.
- Git diff просмотрен; unrelated changes отсутствуют.
- Config/schema/migration changes отражены в handoff.
- OPEN_ISSUES block обновлён.
- Handoff создан и не превышает лимит.
- Next sessions получили точный starting point.
- Финальный ответ краткий и ссылается на handoff.

## 15. Источники для model policy

Проверено 2026-09-10:

- GPT-5.6 Sol: https://developers.openai.com/api/docs/models/gpt-5.6-sol
- GPT-5.6 Terra: https://developers.openai.com/api/docs/models/gpt-5.6-terra
- GPT-5.6 Luna: https://developers.openai.com/api/docs/models/gpt-5.6-luna
- OpenAI reasoning/model guidance: https://developers.openai.com/api/docs/guides/latest-model
- Claude models overview: https://platform.claude.com/docs/en/models/overview
- Claude model selection: https://platform.claude.com/docs/en/about-claude/models/choosing-a-model
- Claude Opus 5: https://platform.claude.com/docs/en/models/opus-5/whats-new-opus-5
- Claude Sonnet 5: https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5
- Claude Fable 5.1: https://platform.claude.com/docs/en/models/fable-5-1/overview
- Claude effort levels: https://platform.claude.com/docs/en/build-with-claude/effort
- Claude Code project memory/CLAUDE.md: https://code.claude.com/docs/en/memory
- Codex AGENTS.md discovery: https://learn.chatgpt.com/docs/agent-configuration/agents-md

Model IDs, access and pricing меняются. Перед новым большим этапом S00/S33 policy следует перепроверить, но уже начатую session нельзя молча переводить на другую модель.
