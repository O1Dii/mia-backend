# MIA MVP: сессии и волны

Статус: рабочий orchestration contract для MVP/PoC
Дата: 2026-10-09
Архитектура: [Mia_Architecture_Review.md](../Mia_Architecture_Review.md) (вариант E)
Хвосты вне MVP: [Mia_Backlog.md](../Mia_Backlog.md)

## 1. Назначение и связь со старым планом

Документ разбивает MVP на автономные **M-сессии**. **Волна** — набор
сессий, которые можно выполнять параллельно: у них нет зависимостей друг
от друга и не пересекаются write scopes.

Идентификаторы `Mxx` и волны `MW0…MW7` не пересекаются со старыми
`Sxx`/`W0…W7`.

Общий контракт сессии наследуется из
[SESSION_EXECUTION_PLAN.md](SESSION_EXECUTION_PLAN.md):

| Раздел | Что берётся |
|---|---|
| §3 | короткий вызов, вход, output, статусы |
| §4 | handoff |
| §5 | OPEN_ISSUES |
| §6 | git и worktrees |
| §12 | repair sessions |
| §13–14 | чеклисты |

Здесь записаны только отличия: модели (§3), ownership (§4), граф (§5),
карточки (§6), gates (§7).

Старые сессии:

| Сессия | Новый статус | Чем заменена или почему |
|---|---|---|
| S28 | `SUPERSEDED` | M20 |
| S31-R1 | `SUPERSEDED` | M22 и M60: голос теперь каскадный, Gemini Live — опция |
| S33 | `SUPERSEDED` | M60 |
| S27 | `PARTIAL / DEFERRED` | Wake word в бэклоге |
| S32 | `PARTIAL` | Историческая запись |

Код и handoffs старых сессий остаются основой и не переписываются без
нужды.

**Изменение scope 2026-10-09 (после MW2, решение пользователя).** Голосовой
режим (M22: каскад, перебивания, кнопка ассистента) остаётся отдельной
функцией. Кроме него обычный чат на web и Android получает голосовой ввод по
кнопке и озвучку ответов. Web и Android переходят на общую дизайн-систему:
иммерсивный объёмный современный отзывчивый стиль. Добавлены сессии M18 (лента
чата, API), M33 (дизайн-система web + Android) и M25 (Android: чат на runs,
голосовой ввод, озвучка, новый UI); M30 расширен голосом и дизайном.

**Gemini Live вне MVP (решение пользователя 2026-10-09).** Код realtime-режима
(backend `/realtime`, адаптер Gemini Live, Android `RealtimeVoiceController`)
сохраняется, но в MVP функция скрыта: Android-флаг `LIVE_VOICE_ENABLED = false`,
пункт в настройках не показывается. M-сессии Live не развивают, не тестируют
вручную и не удаляют; M31 удаляет только `web_poc`. Возврат — пункт бэклога
`Mia_Backlog.md` §4.

**Перед первой волной** coordination-файлы и этот план должны быть
закоммичены в `main`: параллельные сессии создают worktrees от `HEAD`, а
некоммиченные документы в них не попадут. Проверка «working tree clean» из
§13 общего контракта относится и к этому.

Команды пользователя такие же, как раньше:

~~~text
Выполни M10.
Проверь готовность M22.
Выполни все готовые сессии wave MW1 параллельно.
Интегрируй завершённые сессии wave MW1.
~~~

## 2. Репозитории

| Ключ | Путь | Примечание |
|---|---|---|
| B | `/Users/aleksey/PycharmProjects/mia-helper` | backend, web, contracts, coordination |
| A | `/Users/aleksey/PycharmProjects/mia-helper-android` | Android |
| MM | `/Users/aleksey/mind-map-generator` (MindWeaver) | **Не под git.** M51 начинает с вопроса пользователю о `git init` и baseline commit; `.env`, `*.db`, `storage/` идут в `.gitignore`. Без разрешения M51 → `BLOCKED` |

## 3. Model policy

Для разработки доступны Claude **Opus 5.5**, **Sonnet 5.5** и
**Haiku 5.5**. Preferred executor выбирается при открытии сессии. Fallback
используется, только если preferred недоступен или исчерпаны лимиты. Обе
модели на одну сессию не запускаются.

| Модель / effort | Назначение |
|---|---|
| **Opus 5.5 high** | Контракты, identity и безопасность, конкурентность (run engine, router, jobs), аудио, сквозная интеграция |
| **Opus 5.5 xhigh** | Только repair после двух неудачных подходов или трудная диагностика в M60. Не default |
| **Sonnet 5.5 high** | Фичи на стабильном контракте: адаптеры, плагины, UI, Android-экраны, интеграция волн |
| **Sonnet 5.5 medium** | Простые плагины и обёртки, где контракт уже проверен |
| **Haiku 5.5 high** | Документация, шаблоны, фикстуры, механические конвертеры, follow-ups. Не владелец core session |

Эскалация в repair-сессиях:

- Haiku → Sonnet high;
- Sonnet high → Opus high;
- Opus high → Opus xhigh;
- если не помогает и xhigh — новая decision session, а не продолжение.

**Запуск из одной сессии.** Модель выбирается при открытии сессии, текстовая
команда её не переключает. Поэтому:

- при команде на целую волну исполнитель запускает каждую сессию **отдельным
  агентом с моделью из карточки** (Agent tool: `opus` / `sonnet` / `haiku`);
- если сессия уже открыта на более сильной модели (например, Opus 5.5 для
  карточки Sonnet), она может выполнить карточку сама. Фактическая модель
  записывается в handoff. Обратная замена (Sonnet вместо Opus) — только как
  fallback по правилу выше.

**Codex (GPT, подписка Plus)** — запасной исполнитель той же карточки, когда
лимиты Claude исчерпаны. Применяется и как точечный cross-review диффа
M10, M13 и M50 (безопасность). Это не второй обязательный исполнитель.

**Модель продукта (не путать с моделью разработки).** Все сценарии сначала
доводятся на одной основной модели, которую выбирает M01: GPT на ChatGPT
Plus через Sign in with ChatGPT, иначе DeepInfra GLM-5.3-flash. Вторая
модель подключается только в M61.

Context ceiling: 20K — один модуль; 30–35K — фича с границей DB/API/client;
40–45K — runtime, безопасность, аудио; 60–65K — сквозная интеграция.

## 4. Ownership и общие файлы

Каждая сессия работает в своей ветке и worktree, например
`session/M10-unified-accounts`. Активных сессий не больше пяти; по
умолчанию четыре, пятая запускается, когда освободится слот.

| Общий ресурс | Правило |
|---|---|
| `contracts/mvp/v1/*` | Владелец M00. Позже — только аддитивные изменения той сессии, чья карточка их разрешает; изменение фиксируется в handoff |
| `app/core/ports/*` (протоколы ModelGateway, ToolRouter, MemoryProvider, DeviceToolChannel) | Владелец M00. Реализации живут в модулях сессий |
| Миграции Alembic (`migrations/versions/`) | Каждая сессия добавляет **свои** revision-файлы. Integrator волны перестраивает `down_revision` в линейную цепочку в порядке merge |
| `pyproject.toml` / lockfile | Сессия добавляет только свои зависимости. Конфликт lockfile integrator решает перегенерацией |
| `app/composition.py`, подключение роутеров | Только добавление строк регистрации своего модуля; конфликты решает integrator |
| `web/src/lib/api.ts`, `web/src/App.tsx` | M30, затем M31. Остальные запрашивают изменения через handoff |
| `design/**`, `web/src/styles.css`, `web/src/styles/**`, Android `core/designsystem/**` | Владелец M33. Позже M25, M30, M31 используют токены и компоненты; новые токены — аддитивно, с записью в handoff |
| Android `settings.gradle.kts`, `gradle/libs.versions.toml` | Только добавление своих модулей и зависимостей |
| `app/web_poc/*` | Только читается как источник переноса. Удаляет M31 после того, как web полностью переехал |
| `STATUS.md`, индекс `OPEN_ISSUES.md` | Только integrator волны; сессия пишет в свой inbox-блок |

**Integrator волны:**

| Волны | Модель |
|---|---|
| MW1, MW2, MW6 | Opus 5.5 high: миграции, ядро, сквозные проверки |
| MW0, MW3, MW4, MW5, MW7 | Sonnet 5.5 high. В MW0 integrator также переносит решение M01 в ADR-0014 |

Integrator сливает ветки в порядке зависимостей, перестраивает миграции,
запускает gate волны (§7), обновляет STATUS и делает integration commit.

## 5. Граф и волны

~~~text
MW0:  M00 (ADR/контракты) ◄─ —    M01 (spike модели) ◄─ —
MW1:  M10 (аккаунты) ◄─ M00   M11 (gateway) ◄─ M00,M01   M12 (run) ◄─ M00   M20 (VIS) ◄─ M00   M21 (STT) ◄─ M00
MW2:  M14 (builtin) ◄─ M00,M10    M13 (router) ◄─ M10,M12    M22 (голос) ◄─ M12,M20,M21
MW3:  M15 (магазин) ◄─ M13,M14   M16 (jobs) ◄─ M12,M13   M17 (память) ◄─ M10,M11,M12,M13   M23 (device tools) ◄─ M13,M22
      M18 (лента, API) ◄─ M12   M33 (дизайн-система) ◄─ M00
MW4:  M30 (web) ◄─ M15,M18,M33   M25 (Android-чат, голос, UI) ◄─ M18,M22,M33   M40 (Linear) ◄─ M15   M41 (Telegram) ◄─ M15,M16,M17,M23   M50 (SDK/OAuth) ◄─ M11,M12,M13,M17   [M42 Calendar, opt]
MW5:  M31 (web 2) ◄─ M30,M16,M17   M24 (Android web/push) ◄─ M16,M22,M30   M51 (MindWeaver) ◄─ M15,M50   M43 (docs) ◄─ M15,M50
MW6:  M60 (E2E MVP) ◄─ M24,M25,M31,M40,M41,M51
MW6b: M80, M81 (wake word) ◄─ M22; M85…M90 (Personal Node) ◄─ M60, M70 — см. «MW6b» ниже
MW7:  M61 (вторая модель) ◄─ M60      M62 (пилотный деплой) ◄─ M60
~~~

Scheduler может запускать сессию сразу, как только готовы все её
зависимости; ждать конца всей волны не обязательно.

Критические пути:

- backend: M00 → M12 → M13 → M17 → M50 → M51 → M60;
- голос: M00 → M21 → M22 → M23 → M41 → M60.

### Сводная таблица

| ID | Результат | Depends on | Wave | Repo | Preferred | Fallback | Ceiling |
|---|---|---|---|---|---|---|---|
| M00 | ADR и контракты MVP, порты | — | MW0 | B | Opus 5.5 high | Sonnet 5.5 high | 35K |
| M01 | Spike основной модели: GPT-на-Plus против GLM | — | MW0 | B | Sonnet 5.5 high | Opus 5.5 high | 25K |
| M10 | Единые аккаунты и workspace на Postgres | M00 | MW1 | B | Opus 5.5 high | Sonnet 5.5 high | 45K |
| M11 | Model Gateway и хранилище подключений | M00, M01 | MW1 | B | Sonnet 5.5 high | Opus 5.5 high | 40K |
| M12 | Run engine: события, профили, контракт ответа | M00 | MW1 | B | Opus 5.5 high | Sonnet 5.5 high | 45K |
| M20 | Android: системный ассистент (VIS) | M00 | MW1 | A | Sonnet 5.5 high | Opus 5.5 high | 35K |
| M21 | Android: STT на устройстве (GigaAM) | M00 | MW1 | A | Opus 5.5 high | Sonnet 5.5 high | 40K |
| M13 | Tool Router, policy, MCP client, approvals | M10, M12 | MW2 | B | Opus 5.5 high | Sonnet 5.5 high | 45K |
| M14 | Встроенные и декларативные плагины как MCP | M00, M10 | MW2 | B | Sonnet 5.5 high | Opus 5.5 high | 35K |
| M22 | Android: каскадный голос | M12, M20, M21 | MW2 | A | Opus 5.5 high | Sonnet 5.5 high | 45K |
| M15 | Механика магазина и подключений | M13, M14 | MW3 | B | Sonnet 5.5 high | Opus 5.5 high | 40K |
| M16 | Jobs, расписания, входящие, уведомления | M12, M13 | MW3 | B | Opus 5.5 high | Sonnet 5.5 high | 40K |
| M17 | Memory Ledger, люди, приватность V1 | M10, M11, M12, M13 | MW3 | B | Opus 5.5 high | Sonnet 5.5 high | 45K |
| M23 | Android: device tools | M13, M22 | MW3 | A | Sonnet 5.5 high | Opus 5.5 high | 35K |
| M18 | Лента: история основного чата (API) | M12 | MW3 | B | Sonnet 5.5 high | Opus 5.5 high | 25K |
| M33 | Дизайн-система Mia: токены и компоненты web + Android | M00 | MW3 | B, A | Sonnet 5.5 high | Opus 5.5 high | 40K |
| M30 | Web: чат, голосовой ввод и озвучка, подтверждения, магазин | M15, M18, M33 | MW4 | B | Sonnet 5.5 high | Opus 5.5 high | 40K |
| M25 | Android: чат на runs, голосовой ввод, озвучка, новый UI | M18, M22, M33 | MW4 | A | Sonnet 5.5 high | Opus 5.5 high | 40K |
| M40 | Плагин Linear | M15 | MW4 | B | Sonnet 5.5 medium | Sonnet 5.5 high | 25K |
| M41 | Плагин Telegram: L0 + бот | M15, M16, M17, M23 | MW4 | B, A | Sonnet 5.5 high | Opus 5.5 high | 35K |
| M42 | Плагин Google Calendar (**optional**) | M15 | MW4 | B | Sonnet 5.5 medium | Sonnet 5.5 high | 25K |
| M50 | «Войти через Mia», API для приложений, Mia MCP | M11, M12, M13, M17 | MW4 | B | Opus 5.5 high | Sonnet 5.5 high | 45K |
| M24 | Android: web-поверхности и push | M16, M22, M30 | MW5 | A | Sonnet 5.5 high | Opus 5.5 high | 35K |
| M31 | Web: дашборд MCP Apps, память, входящие | M16, M17, M30 | MW5 | B | Sonnet 5.5 high | Opus 5.5 high | 40K |
| M43 | Документация плагинов и SDK, шаблон | M15, M50 | MW5 | B | Haiku 5.5 high | Sonnet 5.5 medium | 20K |
| M51 | MindWeaver ↔ Mia | M15, M50 | MW5 | MM, B | Sonnet 5.5 high | Opus 5.5 high | 40K |
| M60 | Сквозная приёмка MVP и device gate | M24, M25, M31, M40, M41, M51 | MW6 | B, A, MM | Opus 5.5 high | Opus 5.5 xhigh | 65K |
| M61 | Вторая модель и остальные адаптеры | M60 | MW7 | B | Sonnet 5.5 high | Opus 5.5 high | 30K |
| M62 | Пилотный деплой и эксплуатация | M60 | MW7 | B | Sonnet 5.5 high | Opus 5.5 high | 35K |

Распределение моделей:

| Модель | Сколько сессий | Какие |
|---|---|---|
| Opus 5.5 | 10 | Ядро, безопасность, аудио, E2E |
| Sonnet 5.5 | 18 | Фичи, плагины, UI, дизайн-система |
| Haiku 5.5 | 1 | Документация. Также follow-ups по запросу |

## 6. Session cards

Формат карточки:

| Поле | Что в нём |
|---|---|
| Objective | Цель сессии |
| Read | Что читать сверх общего контракта |
| Write scope | Где сессия может менять файлы |
| Work | Что сделать |
| Out of scope | Чего не делать |
| DoD | Обязательная проверка |
| Consumers | Кто использует результат |

Ссылки «Review §N» ведут на `docs/Mia_Architecture_Review.md`.

### M00 — ADR и контракты MVP

**Objective:** зафиксировать решения ревизии как ADR и машинно-проверяемые
контракты, чтобы сессии MW1 могли идти параллельно.

**Read:** Review §0, §5, §7, §8, §11; `contracts/README.md`;
`contracts/plugins/v2/application.schema.json`; `docs/adr/README.md`.

**Write scope:** `docs/adr/0010…0015-*.md`, `contracts/mvp/v1/**`,
`contracts/scripts/validate_contracts.py`, `app/core/ports/**`,
`tests/contracts/mvp/**`, `tests/fakes/**`.

**Work:**

ADR:

| ADR | Тема |
|---|---|
| 0010 | MCP-native плагины (`mia.json` + MCP + Skills + MCP Apps; `builtin` / `remote` / `declarative` / device tools) |
| 0011 | Профили run и контракт ответа speech/display |
| 0012 | Memory Ledger и приватность V1 (per-user DEK, предложенные факты) |
| 0013 | «Войти через Mia» (OAuth 2.1 AS, CIMD, scopes) |
| 0014 | Одна основная модель продукта |
| 0015 | Замена старых S28/S31-R1/S33 |

JSON Schema в `contracts/mvp/v1/`:

- `run-events` (sequence, cursor, типы событий);
- `assistant-response` (`speech`, `display`, `followups`);
- `plugin-manifest` (`mia.json`, Review §5.4.4);
- `approval` (`args_hash`, `expires_at`, `decision`);
- `device-tools` (регистрация, вызов, результат, ошибки);
- `memory` (Fact, Person, Preference);
- `job` / `schedule`.

`contracts/mvp/v1/openapi.yaml` — скелет путей. Ресурсы:

- runs: `/v1/runs`, `/v1/runs/{id}/events`, `/v1/runs/{id}/cancel`;
- подтверждения: `/v1/approvals/{id}/decision`;
- плагины: `/v1/plugins`, `/v1/installations`, `/v1/connections`, `/v1/dashboard`;
- память и фон: `/v1/memory/*`, `/v1/jobs`, `/v1/schedules`, `/v1/inbox`;
- устройства: `/v1/devices/push-token`;
- приложения: `/oauth/*`, `/v1/chat/completions`, `/mcp`.

Python-протоколы в `app/core/ports/`: `ModelGateway`, `ToolRouter`,
`MemoryProvider`, `DeviceToolChannel`, `ApprovalService`. Fake-реализации
лежат в `tests/fakes/`.

Валидатор контрактов охватывает `mvp/v1`.

**Out of scope:** реализация модулей, изменение старых v1/v2-контрактов.

**DoD:**

- `validate_contracts.py` PASS;
- примеры `mia.json` для timer, linear и mindweaver валидны;
- fakes проходят mypy strict;
- ADR ссылаются на Review.

**Consumers:** все.

### M01 — Spike основной модели

**Objective:** выбрать основную модель продукта (Review §5.5).

**Read:** Review §5.5; `app/web_poc/providers.py`; SIWC docs
(developers.openai.com/siwc).

**Write scope:** `spikes/m01/**` (включая `spikes/m01/DECISION.md`). ADR
не трогает: M00 создаёт ADR-0014 с Decision «ожидает M01», а integrator MW0
переносит итог из `DECISION.md` в ADR-0014. Так write scopes M00 и M01 не
пересекаются.

**Work:**

1. Пройти поток Sign in with ChatGPT для локального или
   самостоятельно размещённого приложения. Нужен интерактивный вход
   пользователя своим Plus — это единственный ожидаемый вопрос к
   пользователю.
2. Запрос Responses API (`store:false`, `stream:true`).
3. **Function calling** с двумя tools.
4. Список моделей и наблюдаемые лимиты.
5. Тот же сценарий на DeepInfra GLM-5.3-flash: tools и `reasoning_content`.
   Ключ — переменная окружения `DEEPINFRA_API_KEY` (задокументирована в
   `.env.example`); значение не выводится. Если переменная не задана —
   попросить пользователя задать её, остальные шаги не блокируются.
6. Сравнить задержку первого токена.

**Out of scope:**

- адаптер в продуктовом коде (это M11);
- токены Codex CLI или ChatGPT в обход официального потока.

**DoD:**

- решение «основная = GPT-plan / GLM» с доказательствами;
- если выбран GPT-plan — описан контракт OAuth и запросов для M11;
- никаких секретов в файлах.

**Consumers:** M11, M61.

### M10 — Единые аккаунты и workspace на Postgres

**Objective:** один аккаунт для web (cookie) и Android (bearer); перенести
постоянные данные web POC из SQLite в Postgres.

**Read:** M00 handoff; Gap Analysis §6.2; `app/web_poc/accounts.py`,
`main.py` (части про аккаунты, установки и раскладку); `app/modules/auth`,
`app/modules/users`; OI-WEB-001 и OI-WEB-005.

**Write scope:** `app/modules/users/**`, `app/modules/auth/**`,
`app/modules/workspace/**` (новый), свои миграции, `scripts/migrate_web_sqlite.py`,
`tests/workspace/**`, `tests/auth/**`.

**Work:**

- Связать web Google-вход с `users`, используя проверенный Google `sub`
  (не email). Web cookie session + Android bearer → одна запись `User`.
- Таблицы:
  - `Workspace`;
  - `Installation` (plugin_id, version, grants, tools digest);
  - `PluginState`;
  - `WidgetInstance` (layout для desktop и mobile, settings,
    `state_version`);
  - `Review`.
- API установок и дашборда с `expected_version` (409 при конфликте).
- Скрипт переноса данных web-SQLite с проверкой владельца, идемпотентный.
- Изоляция tenant: негативные тесты, что чужой аккаунт ничего не видит.

**Out of scope:** run engine, магазин (M15), удаление `web_poc`.

**DoD:**

- миграции up/down;
- скрипт переноса на копии тестовой базы;
- кросс-tenant тесты;
- cookie и bearer дают тот же `user_id`;
- ruff, mypy, pytest PASS.

**Consumers:** M13, M14, M15, M17, M30.

### M11 — Model Gateway и хранилище подключений

**Objective:** провайдеро-нейтральный gateway с основной моделью из M01 и
постоянными зашифрованными подключениями пользователя.

**Read:** M00 и M01 handoffs; `app/web_poc/providers.py`, `agent.py`;
`app/providers/ai/**`; OI-W6-013 и OI-WEB-001.

**Write scope:** `app/modules/models/**` (новый), `app/core/crypto.py`
(envelope: master key → per-user DEK), свои миграции, `tests/models/**`.

**Work:**

- Реализация `ModelGateway` из портов M00. Модель данных: `Message`,
  `ContentPart`, `ToolCall`, usage, finish reason, ошибки, cancel; плюс
  непрозрачное provider state (решает OI-W6-013).
- Адаптер основной модели:
  - OpenAI Responses (+ `chatgpt-plan`, если M01 выбрал его);
  - или OpenAI-compatible для DeepInfra GLM.
- Остальные адаптеры из `web_poc` переносятся с меткой `chat_only`.
- Таблица `Connection`: ключи шифруются per-user DEK. CRUD API без
  возврата ключа (только последние 4 символа). Кнопка «Тест API».
- Envelope crypto с ротацией master key и crypto-shred при удалении DEK.

**Out of scope:** контрактные тесты второй модели (M61), биллинг.

**DoD:**

- record/replay-тесты адаптера основной модели: tool loop, ошибки,
  cancel, сохранение provider state;
- ключи не попадают в логи и ответы (тест);
- crypto-shred тест;
- одна реальная smoke-проверка основной модели, если доступен credential.

**Consumers:** M12, M17, M50, M61.

### M12 — Run engine

**Objective:** надёжный run с событиями, профилями и контрактом
speech/display; клиенты получают поток событий.

**Read:** M00 handoff; Review §5.2; `app/modules/orchestration/**`,
`app/modules/conversations/**`; `app/web_poc/agent.py`.

**Write scope:** `app/modules/runs/**` (новый), адаптация
`app/modules/conversations/**` под ленту, свои миграции, `app/api/v1/runs.py`,
`tests/runs/**`.

**Work:**

- `Run` и `RunEvent`: append-only, sequence. SSE `/v1/runs/{id}/events`
  с возобновлением по cursor. Cancel с состоянием `cancel_requested`.
- Профили voice, chat, job, app с лимитами шагов, времени и tool calls.
  Лимиты — в конфиге.
- Цикл: контекст → `ModelGateway` → tool calls через порт `ToolRouter`
  (пока fake) → финальный ответ.
- Парсер контракта ответа: structured output, при его отсутствии —
  эвристика «первая фраза = speech». В `voice` speech ограничен по длине.
- Быстрый «заполнитель»: событие `speech.ack`, если первый ответ не готов
  за N мс.
- Основная лента пользователя + эпизодическая сводка старой части.

**Out of scope:** реальные tools (M13), память (M17), jobs (M16).

**DoD:**

- тесты с fake gateway и fake router: профиль voice режет шаги, cancel,
  reconnect по cursor без потерь и дублей, speech/display в обоих режимах
  модели;
- нагрузочный тест: 20 параллельных run без смешения событий.

**Consumers:** M13, M16, M17, M22, M50.

### M20 — Android: системный ассистент

**Objective:** Mia назначается цифровым ассистентом и запускается жестом,
кнопкой или с экрана блокировки в существующий голосовой экран.
Заменяет S28.

**Read:** M00 handoff; Review §6.1; `feature/assistant/**` (AssistantRoute);
ADR-009; `docs/implementation/DEVICE_GATE.md`.

**Write scope:** A: `feature/assistant/**`, `app/src/main/AndroidManifest.xml`
(записи service), `app/src/main/res/xml/*interaction*`, тесты модуля.

**Work:**

- `VoiceInteractionService` и `VoiceInteractionSessionService` /
  `VoiceInteractionSession`.
- Экран запроса роли через `RoleManager`.
- Запуск голосовой сессии из жеста ассистента; поведение на экране
  блокировки (показать и, если нужно, попросить разблокировать).
- Восстановление после переустановки: роль может сброситься — проверить и
  показать подсказку.

**Out of scope:** wake word, сам голосовой каскад (M22).

**DoD:**

- unit и instrumentation тесты;
- device gate на SM-S938B: назначить роль → долгое нажатие → экран Mia;
  то же на заблокированном экране;
- зафиксированы ограничения OEM.

**Consumers:** M22.

### M21 — Android: STT на устройстве

**Objective:** локальное русское распознавание речи GigaAM-v3 через
sherpa-onnx за интерфейсом `SpeechRecognizer`.

**Read:** M00 handoff; Review §6.2; `core/audio/**`, текущий VAD.

**Write scope:** A: `core/stt/**` (новый модуль), записи в `settings.gradle.kts`
и `libs.versions.toml`, загрузка или упаковка модели (assets или загрузка
при первом запуске с проверкой SHA-256), тесты.

**Work:**

- Интеграция sherpa-onnx.
- GigaAM-v3: CTC или RNNT по замерам.
- Вход — PCM из существующего VAD-сегмента, выход — текст и уверенность.
- Интерфейс с облачным fallback (заглушка).
- Замеры на устройстве: время распознавания 3–5 с речи, память, размер
  APK или модели, WER на 30 фразах (русские имена, смешанная речь).

**Out of scope:** TTS, подключение к run (M22).

**DoD:**

- тесты интерфейса;
- device-замер на SM-S938B с таблицей в handoff;
- решение «упаковать / скачивать модель» записано.

**Consumers:** M22.

### M13 — Tool Router, policy engine, MCP client, approvals

**Objective:** безопасный путь «модель → tool → результат» для MCP-плагинов
и device tools.

**Read:** M00, M10, M12 handoffs; Review §5.3, §5.4.1–5.4.3;
`app/modules/tools/**` (существующий executor, policy, audit); Python MCP SDK
(spec `2026-07-28`).

**Write scope:** `app/modules/tools/**`, `app/modules/plugins/registry/**`
(новый), `app/api/v1/approvals.py`, свои миграции, `tests/tools/**`.

**Work:**

- Реализация `ToolRouter`: загрузка установленных плагинов из
  `Installation` и их `mia.json`.
- MCP client: Streamable HTTP и in-process transport. `server/discover`,
  `tools/list`, `tools/call`; `content` → модели, `structuredContent` →
  событие `display`.
- Discovery: фильтр по профилю (`voice` — только `voice: true` и `read`).
  В промпт идут описания skills, полный `SKILL.md` подгружается при выборе
  плагина.
- Policy:
  - JSON Schema аргументов;
  - grant и scope;
  - `risk` → `Approval` (`args_hash`, `expires_at`; при изменении
    аргументов — новое подтверждение);
  - `operation_id`, receipt, статус `uncertain` при таймауте;
  - allow-list хостов удалённых MCP, запрет private IP и metadata;
  - digest описаний tools: расхождение с установкой → tool блокируется до
    повторного согласия.
- Device tools: маршрутизация вызова на подключённый клиент через
  `DeviceToolChannel` (событие run → ответ клиента → таймаут).
- Аудит без содержимого, продолжение существующей таблицы.
- Встроенные инструменты ядра регистрируются здесь как пустые точки
  расширения для M16 и M17.

**Out of scope:** плагины (M14, M40+), публикация (M15).

**DoD:**

- негативные тесты: неизвестный или чужой tool, неверные аргументы,
  изменённые аргументы после одобрения, истёкшее одобрение, SSRF на
  private IP, подменённое описание tool, prompt injection в результате,
  таймаут → `uncertain` без автоповтора;
- e2e с тестовым MCP-сервером: и in-process, и HTTP.

**Consumers:** M14, M15, M16, M17, M23, M50.

### M14 — Встроенные и декларативные плагины как MCP

**Objective:** перенести плагины web POC в MCP-формат и запустить
декларативные приложения v2 через Mia.

**Read:** M00 и M10 handoffs; Review §5.4.2, §5.4.4; `app/web_poc/runtime.py`,
`extensions.py`, `internet.py`; `contracts/plugins/v1|v2/**`.

**Write scope:** `plugins/builtin/**` (новый: timer, alarm, notes, focus,
internet, news — по каталогу на плагин с `mia.json`, `SKILL.md`,
`server.py`, `ui/*.html`), `app/modules/plugins/declarative/**`,
`scripts/convert_manifest_v2.py`, `tests/plugins/**`.

**Work:**

- Каждый плагин — in-process MCP-сервер (MCP SDK), состояние в
  `PluginState`. Плитка — MCP App HTML (из существующих view).
- Таймер и будильник хранят дедлайн; срабатывание делает M16.
- Декларативный host: tools из `actions`; `state.patch` / `model.prompt`;
  view `fields` / `html` → MCP App.
- Конвертер v1/v2 → `mia.json` с `server.kind: declarative`.
- `risk` и `voice` для каждого tool.

**Out of scope:** регистрация в каталоге (M15), web UI (M30/M31).

**DoD:**

- тесты каждого сервера через MCP-клиента SDK;
- конвертер на примерах `contracts/plugins/**/examples`;
- все `mia.json` валидны по контракту M00.

**Consumers:** M15, M16, M31.

### M22 — Android: каскадный голос

**Objective:** голос с любой моделью: VAD → STT на устройстве →
`Run(voice)` → озвучка speech → карточка display; подтверждения на экране.

**Read:** M12, M20, M21 handoffs; Review §6.2; `feature/voice/**`
(RealtimeVoiceController), `core/audio/**`; OI-W6-005 и OI-W6-017.

**Write scope:** A: `feature/voice/**`, `core/tts/**` (новый),
`core/network/**` (SSE-клиент runs), `feature/approvals/**` (новый), тесты.

**Work:**

- Контроллер каскада рядом с Live: Live остаётся опцией в настройках.
- SSE-клиент `/v1/runs`. TTS (Android `TextToSpeech`) по предложениям,
  `speech.ack` — сразу.
- Barge-in: остановка TTS и cancel run.
- Рендер `display`-карточки и экрана Approval (одобрить, отклонить,
  истекло).
- Регистрация клиента как `DeviceToolChannel` (сам канал, без tools — их
  добавляет M23).

**Out of scope:** device tools, push, WebView-экраны.

**DoD:**

- тесты контроллера на fake STT, fake SSE и fake TTS;
- device gate SM-S938B:
  - 20 ходов подряд;
  - p50 и p95 «конец речи → первый звук» (таблица по звеньям);
  - barge-in;
  - reconnect;
  - подтверждение из голоса.

**Consumers:** M23, M24, M41, M60.

### M15 — Механика магазина и подключений

**Objective:** полный цикл: опубликовать → найти → установить с согласием →
подключить аккаунт → обновить или удалить; плюс «добавить свой MCP по URL».

**Read:** M13 и M14 handoffs; Review §5.4.5; `web/src/plugins/catalog.ts`
(как данные витрины).

**Write scope:** `app/modules/plugins/catalog/**`, `app/modules/plugins/install/**`,
`app/modules/connections/oauth/**`, `cli/mia_plugin.py`, `app/api/v1/plugins.py`,
свои миграции, `tests/catalog/**`.

**Work:**

- `mia plugin publish <dir>`: валидация `mia.json`; `server/discover`;
  совпадение tools с manifest; CSP UI; digest; версия → `PluginRelease`
  (только для админа).
- Каталог: поиск, категории, страница, скриншоты, отзывы. Источник —
  `PluginRelease`; витринные данные переносятся из `catalog.ts`.
- Установка: экран согласия (tools, risk, данные, network, LLM) →
  `Installation` с закреплённым release.
- OAuth для удалённых MCP: Protected Resource Metadata, PKCE, Resource
  Indicator, хранение токенов (crypto из M11), refresh, отзыв.
- «Добавить MCP по URL»: приватная установка в свой аккаунт без каталога.
- Обновление с повторным согласием при расширении прав. Удаление: отзыв
  токенов, удаление плиток, опция удалить данные.

**Out of scope:** конкретные внешние плагины (M40+), web-экраны (M30).

**DoD:**

- публикация builtin-плагинов из M14 PASS;
- add-by-URL с тестовым HTTP MCP-сервером, включая OAuth (тестовый AS);
- повторное согласие при расширении прав;
- удаление отзывает токен.

**Consumers:** M30, M40, M41, M42, M43, M51.

### M16 — Jobs, расписания, входящие, уведомления

**Objective:** модель отправляет работу в фон; расписания переживают
рестарт; результаты доходят до пользователя.

**Read:** M12 и M13 handoffs; Review §5.2(d); OI-WEB-002.

**Write scope:** `app/modules/jobs/**`, `app/worker/**` (новый процесс),
`app/modules/notifications/**`, `app/api/v1/jobs.py`, `app/api/v1/inbox.py`,
`app/api/v1/devices.py` (push token), свои миграции, `tests/jobs/**`;
запись `worker` в `docker-compose`.

**Work:**

- Очередь на Postgres (Procrastinate или `SKIP LOCKED`): claim/lease,
  retry, история запусков.
- Встроенные tools:
  - `background.start`, `background.status`, `background.cancel`;
  - `schedule.create` (RRULE + IANA TZ, политика пропусков skip,
    run_once или limited_catch_up), `schedule.pause`, `run_now`;
  - `notify`.
- Срабатывание таймеров и будильников из M14 через планировщик.
- Run профиля `job`. Результат → `InboxItem` → push (FCM HTTP v1; токены
  устройств) и, если подключён, Telegram-бот (точка расширения для M41).
- Сводка «пока вас не было» для следующего голосового run.

**Out of scope:** Android-обработка push (M24), UI (M31).

**DoD:**

- расписание переживает рестарт worker;
- пропуски по политике;
- отмена;
- двойной claim невозможен (тест конкуренции);
- push отправляется на тестовый токен (fake FCM).

**Consumers:** M24, M31, M41.

### M17 — Memory Ledger, люди, приватность V1

**Objective:** Mia помнит людей, факты и предпочтения прозрачно для
пользователя (Review §7.3, §7.5).

**Read:** M10–M13 handoffs; Review §7.

**Write scope:** `app/modules/memory/**`, `app/api/v1/memory.py`, свои
миграции, `tests/memory/**`.

**Work:**

- Сущности `Fact`, `Person` (aliases, relation, identifiers), `Preference`.
  Поля source, sensitivity, `last_used_at`. Содержимое шифруется per-user
  DEK.
- Tools:
  - `memory.remember` → в «предложенные»; автосохранение только для
    разрешённых категорий;
  - `people.resolve`: память → device tool `device.contacts.search` (порт;
    клиент добавляет M23) → кандидаты. Модель видит `person:<id>`,
    идентификаторы подставляются при исполнении.
- Сбор контекста для run: закреплённый профиль + top-N фактов через
  Postgres FTS; минимизация — без телефонов и ID.
- Инкогнито-run; retention истории; экспорт `/memories/*.md` и JSON;
  импорт в очередь проверки.
- Удаление аккаунта = crypto-shred DEK + удаление строк; журнал доступа к
  памяти.

**Out of scope:** E2EE V2, экраны (M31), scopes приложений (M50).

**DoD:**

- «Запомни, что Яна — моя жена» → предложенный факт → подтверждение →
  `people.resolve("Яна")` возвращает person;
- провайдеру не уходит телефон (тест на собранный контекст);
- инкогнито не пишет;
- экспорт и импорт;
- crypto-shred.

**Consumers:** M31, M41, M50.

### M23 — Android: device tools

**Objective:** телефон исполняет действия ядра: контакты, будильник,
открыть приложение, передача в Telegram (L0).

**Read:** M13 и M22 handoffs; контракт `device-tools` из M00; Review §7.5.

**Write scope:** A: `core/devicetools/**` (новый), записи о разрешениях в
манифесте, тесты.

**Work:**

- Регистрация tools при подключении к run:
  - `device.contacts.search` (READ_CONTACTS по запросу, только результаты
    поиска);
  - `device.alarm.set` (AlarmClock intent);
  - `device.app.open`;
  - `telegram.handoff` (deep link или share intent с текстом к контакту).
- Ответы с ошибками разрешений.
- Ничего из контактов не сохраняется на сервере.

**Out of scope:** TDLib (бэклог), push (M24).

**DoD:**

- instrumentation-тесты;
- device gate: найти контакт по имени, поставить будильник голосом,
  открыть Telegram с текстом.

**Consumers:** M41, M60.

### M18 — Лента: история основного чата (API)

**Objective:** клиенты показывают одну ленту пользователя (голос и чат) с
историей и подгрузкой, а не одноразовый чат.

**Read:** M12 handoff (основная лента, `FeedService`); `app/modules/conversations/feed.py`,
`app/api/v1/runs.py`; `contracts/mvp/v1/openapi.yaml`.

**Write scope:** `app/api/v1/feed.py` (новый), методы чтения в
`app/modules/conversations/feed.py` (только добавление), аддитивно
`contracts/mvp/v1/openapi.yaml` (`GET /v1/feed`) и `error-codes.json`,
регистрация в `app/composition.py`, `tests/feed/**`.

**Work:**

- `GET /v1/feed?before=<cursor>&limit=` → элементы ленты: `id`, `role`,
  `source` (`run_voice` / `run_chat` / `run_job` / …), `text`, `display`
  (если был), `run_id`, `created_at`; новые сверху, keyset-курсор.
- Bearer и cookie-principal M10 (web и Android); только своя лента.
- Инкогнито-run не попадает в ленту (как в M12).

**Out of scope:** UI (M25, M30), поиск по истории, редактирование.

**DoD:**

- пагинация без пропусков и дублей при одновременной записи;
- изоляция пользователей (чужой курсор → 400, чужие элементы недоступны);
- голос и чат в одной ленте; `validate_contracts.py` PASS.

**Consumers:** M25, M30, M31.

### M33 — Дизайн-система Mia: web + Android

**Objective:** один визуальный язык для web и Android: иммерсивный,
объёмный, современный, отзывчивый. Один источник токенов и общие
компоненты, на которые переходят M25, M30, M31.

**Read:** M00 handoff; `web/src/styles.css`, `web/src/components/**` (только
чтобы увидеть текущие экраны); Android `core/designsystem/**`,
`feature/voice/**` (экран M22), `feature/chat/**`.

**Write scope:** `design/**` (новый: `tokens/mia.tokens.json`, генератор,
style-страница), `web/src/styles.css`, `web/src/styles/**` (новый), в
`web/src/components/**` только разметка и классы без изменения логики;
Android `core/designsystem/**`; тесты своих модулей.

**Work:**

- Направление (решение диспетчера по запросу пользователя; уточняется на
  ревью):
  - тёмная тема — основная, светлая — полноценная;
  - фон — мягкий подвижный градиент («аврора»);
  - поверхности — слоистое «стекло»: полупрозрачность и размытие, тонкий
    световой кант, тени по уровням глубины;
  - крупные скругления, выразительная, но спокойная типографика;
  - движение — пружинные переходы и микроанимации, `prefers-reduced-motion`
    и системная настройка анимаций Android соблюдаются;
  - общий визуальный «голосовой шар» (orb), реагирующий на уровень
    звука, — для голосового режима и кнопки микрофона;
  - контраст не ниже WCAG AA, касание ≥ 44/48 dp.
- Токены в формате W3C Design Tokens (`design/tokens/mia.tokens.json`):
  цвета, градиенты, глубина, радиусы, отступы, типографика, движение,
  брейкпоинты. Скрипт `design/scripts/build_tokens.py` генерирует
  `web/src/styles/tokens.css` и Android `MiaTokens.kt`; проверка в тестах,
  что сгенерированное не расходится с источником.
- Web: перевести `styles.css` на токены, применить новый стиль к текущим
  экранам; адаптивность 390 / 768 / 1440 px.
- Android (Compose): `MiaTheme` на токенах; компоненты — glass-поверхность,
  кнопки, поле ввода с кнопкой микрофона, пузырь сообщения, карточка
  `display`, orb; запасной вариант без размытия для API < 31.
- Style-страница `design/preview/index.html` (все токены и компоненты, обе
  темы) — для ревью пользователем.

**Out of scope:** логика экранов, перенос на новый API (M25, M30), новые
экраны.

**DoD:**

- генерация токенов воспроизводима, тест расхождения PASS;
- web: production build, TypeScript, Prettier PASS; скриншоты текущих
  экранов в обеих темах на 390 / 768 / 1440 px без горизонтального скролла;
- Android: `lint test assembleDebug` PASS, screenshot/preview-тесты
  компонентов в обеих темах;
- контраст основных пар текст/фон ≥ 4.5:1 (тест по токенам);
- в `USER_CHECKS.md` добавлена проверка «ревью дизайна» со ссылкой на
  style-страницу и скриншоты.

**Consumers:** M25, M30, M31, M24.

### M30 — Web: чат, подтверждения, магазин

**Objective:** web работает на едином ядре: чат через runs с голосовым
вводом и озвучкой, подтверждения, магазин и подключения — в стиле M33.

**Read:** M15, M18, M33 handoffs (и M12/M13 через них); `web/src/**`;
`web/README.md`.

**Write scope:** `web/src/lib/api.ts`, `web/src/App.tsx`,
`web/src/components/{Chat,Models,PluginPage,LinkedApps,Dialog}.tsx`,
`web/src/components/Approvals.tsx` (новый), `web/src/components/Store*.tsx`,
`web/tests/**`.

**Work:**

- Клиент `/v1/runs` SSE с переподключением; лента (`GET /v1/feed`, M18)
  вместо одноразового чата.
- Карточки display; экран Approval.
- Магазин на новом API: установка с согласием, OAuth-подключение,
  «добавить MCP по URL», обновление и удаление.
- Подключения моделей на API M11.
- Голосовой ввод по кнопке и озвучка ответа переносятся на runs (сейчас —
  браузерные `SpeechRecognition`/`speechSynthesis` в `web/src/lib/voice.ts`):
  кнопка микрофона в поле ввода → текст в поле (отправка вручную, настройка
  «отправлять сразу»); у каждого ответа «Озвучить/Остановить»; если вопрос
  был надиктован, ответ (`response.final.speech`) озвучивается автоматически;
  настройка «Всегда озвучивать ответы». Браузер без распознавания — кнопка
  скрыта с подсказкой.
- Все экраны M30 — на компонентах и токенах M33, адаптивно (телефон,
  планшет, десктоп).

**Out of scope:** дашборд, память, входящие (M31).

**DoD:**

- Playwright: установить плагин → подключить (тестовый OAuth) → вызвать
  в чате → подтверждение → результат;
- add-by-URL;
- Playwright с фейковыми `SpeechRecognition`/`speechSynthesis`: диктовка →
  текст в поле; автоозвучка после надиктованного вопроса; кнопка «Озвучить»;
- скриншоты ключевых экранов на 390 / 768 / 1440 px без горизонтального
  скролла;
- production build, TypeScript, Prettier PASS.

**Consumers:** M24, M31.

### M25 — Android: чат на runs, голосовой ввод, озвучка, новый UI

**Objective:** обычный чат на телефоне работает на едином ядре, принимает
голосовой ввод по кнопке и озвучивает ответы; все основные экраны — в
стиле M33. Голосовой режим M22 остаётся отдельной функцией.

**Read:** M18, M22, M33 handoffs (M12/M13 через них); `feature/chat/**`,
`feature/conversation-list/**`, `feature/settings/**`, `core/network/**`
(клиент runs M22), `core/stt/**`, `core/tts/**`.

**Write scope:** A: `feature/chat/**`, `feature/conversation-list/**`,
`feature/settings/**`, `feature/auth/**` (только UI), UI-композаблы
`feature/voice/**` и `feature/approvals/**` (контроллеры не менять),
навигация `app/**`, тесты.

**Work:**

- Чат на `/v1/runs` (`profile: chat`) и ленте `GET /v1/feed`: потоковый
  текст, карточки `display`, подтверждения прямо в ленте, история с
  подгрузкой; старый API разговоров больше не используется экраном чата.
- Голосовой ввод: кнопка микрофона в поле ввода → STT на устройстве (M21)
  с VAD-окончанием → текст в поле для правки; отправка вручную, настройка
  «Отправлять сразу». Если модель STT не скачана — то же согласие, что в
  M22 (OI-M21-002).
- Озвучка: «Озвучить/Остановить» у каждого ответа (полный текст);
  автоозвучка `response.final.speech`, если вопрос был надиктован;
  настройка «Всегда озвучивать ответы». Новое сообщение или микрофон
  останавливают озвучку.
- Голосовой режим — отдельная кнопка в чате и вызов ассистентом (M20/M22),
  его логика не меняется. Gemini Live остаётся скрытым (`LIVE_VOICE_ENABLED`).
- Новый UI на компонентах M33: чат, лента/список, настройки, вход, экран
  голосового режима (orb), подтверждение; отзывчивая вёрстка (телефон,
  планшет, альбомная ориентация).

**Out of scope:** device tools (M23), WebView и push (M24), изменения
backend.

**DoD:**

- тесты на fake STT, fake TTS и fake runs: диктовка → текст в поле,
  автоозвучка после диктовки, кнопка «Озвучить», остановка при новом
  вводе, подтверждение в ленте, подгрузка истории;
- screenshot/preview-тесты экранов в обеих темах;
- `lint test assembleDebug` PASS;
- device gate SM-S938B: надиктовать вопрос → ответ в ленте и озвучен;
  «Озвучить» у старого ответа; переход в голосовой режим и обратно; в
  `USER_CHECKS.md` — проверка внешнего вида и ощущения от UI.

**Consumers:** M24, M60.

### M40 — Плагин Linear

**Objective:** проверить механику магазина на внешнем удалённом MCP с
собственной плиткой.

**Read:** M15 handoff; Review §5.4.4.

**Write scope:** `plugins/linear/**` (`mia.json`, `SKILL.md`, `ui/my-issues.html`),
`tests/plugins/linear/**`.

**Work:**

- `mia.json` на официальный MCP Linear (OAuth); `risk` и `voice`;
  bundled-плитка «Мои задачи» с refresh через `list_issues`.
- Публикация через `mia plugin publish`.

**Out of scope:** собственный MCP-сервер Linear.

**DoD:**

- публикация PASS;
- контрактный тест на записанных ответах;
- один реальный smoke с аккаунтом пользователя, если он доступен;
  иначе `PARTIAL` только по smoke.

**Consumers:** M60.

### M41 — Плагин Telegram: L0 и бот

**Objective:** «Напиши Яне в Телеграм, что надо купить молоко» работает
голосом; бот Mia доставляет уведомления.

**Read:** M15, M16, M17, M23 handoffs; Review §7.5.

**Write scope:** B: `plugins/telegram/**`, `app/modules/notifications/telegram.py`,
`tests/plugins/telegram/**`. A: только если нужна правка `telegram.handoff`,
и тогда через запрос в handoff M23-R.

**Work:**

- Tool `telegram.send_message {to: person, text}` (`risk:
  write_external`): `people.resolve` → Approval → исполнение через device
  tool `telegram.handoff` (L0).
- Бот: привязка по deep link `/start <token>`, канал `notify` для
  M16. Webhook с секретом.
- `SKILL.md` с примерами русских формулировок.

**Out of scope:** TDLib/L1 и L2 (бэклог).

**DoD:**

- e2e (fake device, fake bot API): имя → уточнение при двух Янах →
  подтверждение → handoff;
- отказ или истечение → ничего не отправлено;
- device gate голосом;
- результат job приходит ботом.

**Consumers:** M60.

### M42 — Плагин Google Calendar (optional)

**Objective:** «Что у меня сегодня?» с календарём. Выполнять, если остаётся
ёмкость; M60 не ждёт эту сессию.

**Write scope:** `plugins/google-calendar/**`, `tests/plugins/google_calendar/**`.

**Work:** read-only события дня через Google Calendar API (OAuth, sensitive
scope, testing mode); плитка «Сегодня»; `voice: true`.

**DoD:** контрактные тесты; smoke в testing mode при наличии разрешения
пользователя.

### M50 — «Войти через Mia», API для приложений, Mia MCP

**Objective:** сторонние приложения входят через Mia и используют модель,
память и инструменты пользователя по согласию (Review §8).

**Read:** M11, M12, M13, M17 handoffs; Review §8.1–8.2; Authlib docs.

**Write scope:** `app/modules/oauth/**`, `app/api/v1/openai_compat.py`,
`app/api/mcp_server.py`, `sdk/js/**`, `sdk/python/**`, web-страница
согласия `web/src/oauth/**` (отдельный entry, не трогает `App.tsx`), свои
миграции, `tests/oauth/**`.

**Work:**

- OAuth 2.1 AS: authorization code + PKCE, CIMD client ID, refresh,
  отзыв.
- Scopes: `openid`, `profile`, `email`, `llm.generate`, `memory.context`,
  `memory.read:<cat>`, `memory.suggest`, `tools.use:<plugin>`, `jobs`,
  `notify`, `mia.install`.
- Экран согласия. Месячный лимит денег на приложение, журнал расходов и
  доступа.
- `/v1/chat/completions` и `/v1/responses` (OpenAI-compatible) →
  `Run(app)`, `model: "mia:default"`, расширения в `extra_body`.
- `/mcp`: Mia как MCP-сервер (память по scopes, tools установленных
  плагинов).
- SDK: JS (PKCE, runs, completions) и Python, генерация из OpenAPI.

**Out of scope:** платные приложения, выдача ключа провайдера.

**DoD:**

- конформанс-тесты OAuth: PKCE, отказ без scope, отзыв, CIMD;
- OpenAI Python SDK и LiteLLM работают с `base_url` Mia;
- лимит расходов срабатывает;
- `memory.context` без `memory.read` не отдаёт сырые факты.

**Consumers:** M43, M51.

### M24 — Android: web-поверхности и push

**Objective:** магазин, дашборд, память и подключения доступны в
Android-приложении; push с результатами фоновых задач.

**Read:** M16, M22, M30 handoffs.

**Write scope:** A: `feature/web/**` (новый), `core/push/**` (новый),
навигация `app/**` (только добавление пунктов), тесты.

**Work:**

- WebView со входом через bearer → обмен на web-сессию (одноразовый код,
  endpoint добавляет M10-R или M30 по запросу в handoff).
- Изоляция WebView (без общего JS bridge, кроме разрешённых действий).
- FCM: регистрация токена, уведомления о входящих, открытие результата.

**Out of scope:** нативные экраны магазина и дашборда.

**DoD:**

- device gate: открыть магазин и установить плагин на телефоне;
- push о завершённом job открывает карточку.

**Consumers:** M60.

### M31 — Web: дашборд MCP Apps, память, входящие

**Objective:** плитки — это MCP Apps; страницы «Что Mia знает» и
«Входящие»; удаление `web_poc`.

**Read:** M16, M17, M30 handoffs; `web/src/plugins/ViewBridge.tsx`,
`web/src/components/Dashboard.tsx`; MCP Apps spec (AppBridge,
`@mcp-ui/client`).

**Write scope:** `web/src/components/{Dashboard,Memory,Inbox,Privacy}.tsx`,
`web/src/plugins/**`, `web/src/lib/api.ts` (после M30), `web/src/App.tsx`
(после M30), удаление `app/web_poc/**` и связанных тестов после проверки
паритета, `web/tests/**`.

**Work:**

- MCP Apps host: sandboxed iframe на отдельном origin, CSP из `_meta.ui`,
  JSON-RPC bridge (`tools/call` через host с policy).
- Плитки из `WidgetInstance` (размеры, mobile card, refresh).
- Страница памяти: люди, факты, предложенные; правка, удаление, экспорт и
  импорт.
- Входящие и задачи: статусы, пауза, запустить сейчас.
- Страница «Где ваши данные».

**Out of scope:** E2EE, новые плагины.

**DoD:**

- Playwright: плитка таймера обновляется без LLM;
- iframe не читает cookies и DOM host (негативный тест);
- память — подтвердить, удалить, экспорт;
- `web_poc` удалён, 71 прежний тест перенесён или заменён эквивалентами.

**Consumers:** M24, M60.

### M43 — Документация плагинов и SDK

**Objective:** автор (команда) делает плагин по шаблону за час.

**Read:** M15 и M50 handoffs; `docs/PLUGIN_DEVELOPMENT.md`.

**Write scope:** `docs/PLUGIN_DEVELOPMENT.md`, `plugins/_template/**`,
`sdk/*/README.md`, `docs/SDK_QUICKSTART.md`.

**Work:**

- Переписать руководство под `mia.json` + MCP + Skills + MCP Apps.
- Шаблон плагина с тестом.
- Quickstart «Войти через Mia».

**DoD:** шаблон публикуется через `mia plugin publish` в тестовом окружении;
ссылки валидны.

### M51 — MindWeaver ↔ Mia

**Objective:** основной сценарий MindWeaver идёт через Mia (Review §8.3).

**Read:** M15 и M50 handoffs; `~/mind-map-generator/README.md`,
`backend/MCP_SETUP.md`, `backend/app/services/{llm,mcp_ops,auth}.py`,
`backend/app/mcp_server.py`. Секретные файлы (`.env`, `deploy/*.env*`) не
открывать.

**Write scope:** MM: `backend/app/services/llm.py` (провайдер `mia/*`),
`backend/app/services/mia_auth.py` (новый), `backend/app/routers/mcp_http.py`
(новый), `backend/app/routers/auth.py` (кнопка входа),
`frontend/src/pages/Login.jsx`, `frontend/src/pages/Settings.jsx`,
`mia/` (новый: `mia.json`, `SKILL.md`, `ui/map.html`), тесты. B: только
handoff.

**Work:**

- Preflight: git (§2).
- «Войти через Mia» (SDK из M50).
- LiteLLM `mia/*` с токеном пользователя; собственные ключи становятся
  необязательными.
- Hosted MCP (Streamable HTTP, `2026-07-28`) поверх `mcp_ops` с проверкой
  токена Mia.
- MCP App превью карты.
- Установка в Mia через add-by-URL, затем публикация.

**Out of scope:** деплой MindWeaver на `mindmapgen.xyz` (отдельно, по
разрешению пользователя).

**DoD:**

- локально: «Мия, сделай карту по этому тексту» → job → карта создана
  моделью пользователя через Mia → плитка;
- отзыв гранта в Mia → MindWeaver получает 401;
- существующие тесты MindWeaver PASS.

**Consumers:** M60.

### M60 — Сквозная приёмка MVP

**Objective:** доказать, что MVP работает целиком на основной модели; то,
что не работает, зафиксировано как repair-сессии.

**Read:** handoffs всех зависимостей; Review §10.2; `DEVICE_GATE.md`.

**Write scope:** `tests/e2e_mvp/**`, `docs/implementation/MVP_ACCEPTANCE.md`,
исправления только в пределах safe fix (до ~50 строк на дефект, иначе
repair-сессия).

**Work:** сценарии.

| # | Сценарий |
|---|---|
| 1 | Магазин: опубликовать → установить → подключить → плитка → голос |
| 2 | «Что у меня сегодня» на доступных плагинах (Linear, заметки, таймеры; Calendar, если M42 выполнен) |
| 3 | «Напиши Яне…» с уточнением и подтверждением |
| 4 | «Напоминай о просроченных задачах по будням в 9» — рестарт worker, доставка |
| 5 | Фоновая задача в голосе и сообщение «пока вас не было» |
| 6 | MindWeaver голосом и из web |
| 7 | Память: просмотр, удаление, инкогнито, удаление аккаунта |
| 8 | Негативные проверки безопасности (Review §5.3, ревизия §24 GPT) |
| 9 | Голосовые замеры: p50/p95, 20 ходов, barge-in, батарея за час |
| 10 | Чат с голосовым вводом и озвучкой на web и телефоне; единый стиль на 390 / 768 / 1440 px |

**DoD:**

- `MVP_ACCEPTANCE.md` с результатами по каждому сценарию;
- статус MVP: `READY` / `NOT READY`;
- список repair-сессий `Mxx-Rn`.

**Consumers:** M61, M62.

### M61 — Вторая модель и остальные адаптеры

**Objective:** подключить вторую модель (GLM или GPT, обратную выбору M01)
и проверить остальные адаптеры.

**Write scope:** `app/modules/models/**` (исправления адаптеров),
`tests/models/contract_suite/**`, таблица профилей.

**Work:** прогнать контрактный набор M60 на второй модели; исправить
адаптер; пометить «проверено» или «чат-совместимо». Остальные адаптеры —
smoke чата.

**DoD:** таблица профилей с доказательствами; основная модель не
деградировала.

### M62 — Пилотный деплой и эксплуатация

**Objective:** MVP доступен 5–20 пилотным пользователям на одной VM.

**Write scope:** `deploy/**`, `ops/**`, `docs/PILOT_RUNBOOK.md`.

**Work:**

- Docker Compose: api, worker, postgres, reverse proxy.
- Бэкапы Postgres вне VM и проверка восстановления.
- Мастер-ключ шифрования вне БД.
- Метрики без содержимого.
- Rollback.
- Публикация — только с явного разрешения пользователя на внешний
  сервер.

**DoD:**

- restore drill PASS;
- smoke на сервере;
- runbook.

### M62-T1 — Тестовый деплой на mindmapgen.xyz/mia и APK для скачивания

Добавлена 2026-10-10 по решению пользователя: тестовый деплой раньше M60, чтобы проверять без
USB и с публичным https. mindmapgen.xyz — не production. Полный пилот (бэкапы вне VM, restore
drill, метрики) остаётся в M62.

**Objective:** текущий MVP (API, worker, Postgres, Redis, web) работает на
`https://mindmapgen.xyz/mia/` вместо старого POC; на web есть «Скачать для Android»; модель
ChatGPT по подписке подключается из интерфейса.

**Write scope:** B: `deploy/**`, `Dockerfile*`, `docker-compose.yml`, `app/main.py` (раздача
web), настройки пути cookie/issuer в `app/core/config.py` и `app/modules/auth/**` (только
cookie path/trusted origins), `app/modules/models/chatgpt_plan/**` + `router.py` (вход по коду
и вставка адреса), `web/src/components/Models.tsx`, `web/src/lib/{api,http}.ts`, место кнопки
APK в web (`More`/`Privacy`), маршрут `/downloads/mia.apk`, тесты, `docs/DEPLOY_TEST.md`.
A: только сборка APK с `-PmiaBackendBaseUrl=https://mindmapgen.xyz/mia` (без правок кода).

**Решения пользователя:** выкладка на внешний сервер разрешена (замена POC на `/mia/`,
nginx `/mia`, карточка хаба, перезапуск только сервиса mia; пароль сервера — из общего
deploy-скрипта как переменная окружения, без вывода); данные POC не переносить (SQLite —
резервная копия на сервере); APK — debug-вариант `by.mia.assistant.debug`, хранить на сервере
в томе, не в образе; ChatGPT — вход из «Модели» (device code, запасной путь — вставка адреса).

**DoD:** smoke на сервере (health, вход Google на web, run с моделью, SSE через nginx,
worker); cookie ограничены `/mia/`; другие приложения сервера отвечают как до выкладки;
APK скачивается и входит на телефоне без USB; rollback к прежнему образу проверен или
описан; память сервера после выкладки записана.

### Волна MW5b — по итогам живой проверки (добавлена 2026-10-10)

Решения пользователя после проверки на https://mindmapgen.xyz/mia/: ADR-0016 (облако по
умолчанию, действия и данные устройства — по запросу и с согласием на устройстве), ADR-0017
(отдельные чаты, общее состояние аккаунта), встроенный поиск, короткие вопросы без фоновой
задачи, журнал вызовов инструментов, полоса «Сейчас». Wake word — после MVP (ADR-0009).

Порядок: сначала repair **M41-R1** и **M15-R1** (баги живой проверки); **MW5b-1** — M70, M73, M74 параллельно (backend); **MW5b-2** — M71, M72, M75, M76, M77
после своих зависимостей. Ownership: `app/modules/tools/router.py` и `device.py` — M70;
`app/modules/runs/**` (промпты, события) — M73; `app/modules/conversations/**`, `app/api/v1/feed.py`
и миграции чатов — M74; `contracts/mvp/v1/*` — аддитивно, каждая сессия свои пути.

#### M41-R1 — Ссылки «только на экран» из результатов инструментов (repair)

**Objective:** «подключи Telegram-бота» показывает кнопку со ссылкой на web и Android; ссылка не
попадает в текст модели. **Preferred:** Opus 5.5 high (repair Sonnet → Opus). **Depends on:** — (main).
**Причина (2026-10-10):** `builtin.telegram.connect_bot` кладёт `deep_link` в `structuredContent`, а
ни web, ни Android такие данные не выводят — ссылка теряется.
**Write scope:** `app/modules/plugins/builtin/kit.py` (общий формат действия для экрана, например
`_meta["mia/action"] = {kind: "open_url", url, label, expires_at}`), `app/plugins/builtin/telegram/**`,
передача действия в событии шага `tool.call.completed` (только UI, не модели) —
`app/modules/tools/router.py` и `app/modules/runs/**` минимальной правкой, `contracts/mvp/v1/*`
аддитивно; web `web/src/components/ChatParts.tsx` (кнопка на шаге), Android `feature/chat/**` (кнопка),
тесты. **DoD:** e2e: connect_bot → событие с действием, в истории модели ссылки нет; Playwright —
кнопка «Открыть Telegram» в чате; Android JVM/Compose тест кнопки; живая проверка — UC-M41-2.

#### M15-R1 — Сервер по URL с большим числом инструментов; видимость ошибок (repair)

**Objective:** Linear (76 tools) по URL работает; ни одна установка не выпадает молча.
**Preferred:** Opus 5.5 high. **Depends on:** — (main).
**Причина (2026-10-10):** add-by-URL сохранил манифест со всеми 76 инструментами, `parse_manifest`
требует 1..64 → `InstalledPluginRegistry._resolve` пропускает установку, модель не видит Linear;
логи заменяют имена событий на `redacted_event`.
**Write scope:** `app/modules/plugins/registry/**`, `app/modules/plugins/catalog/**` (add-by-URL),
`app/modules/tools/**` (только если нужен лимит набора), логирование (`app/core/logging*` — имена событий
не редактировать, редактировать только значения), web `web/src/components/StoreAddUrl.tsx`
(выбор инструментов/сообщение), `contracts/mvp/v1/*` аддитивно, тесты.
**Work:** согласованный лимит на всех шагах; при > лимита — выбор нужных инструментов на экране
согласия (по умолчанию — чтение и основные), либо поднять лимит для custom с учётом контекста модели —
решить и записать; установка с невалидным манифестом получает статус `needs_attention` и видна в
«Мои плагины», а не пропадает; имена событий в логах читаемы.
**DoD:** тест с фейковым сервером на 76 инструментов: добавление → выбор → вызов из чата; существующая
установка Linear на тестовом сервере начинает работать после выкладки (миграция/пересогласие описаны).

#### M77 — Каналы уведомлений, настраиваемые пользователем

**Objective:** пользователь сам решает, куда приходят уведомления; новые каналы появляются без правок
ядра. Решение пользователя 2026-10-10 (OI-M41-002 DECIDED). **Preferred:** Sonnet 5.5 high.
**Depends on:** M70 (владелец `app/modules/notifications/**` в MW5b-1).
**Write scope:** `app/modules/notifications/**` (настройки, маршрутизация доставки), `app/modules/jobs/service.py`
(`channels_of`), `app/plugins/builtin/telegram/**` и бот (вопрос при подключении), реестр каналов,
`contracts/mvp/v1/*` (аддитивно: настройки, `provides.notificationChannel` в манифесте как точка
расширения), своя миграция, web `web/src/components/` (страница «Уведомления»), тесты. Android-часть — в M71.
**Work:**
- «Входящие» — всегда; по умолчанию у новых пользователей ещё только push.
- Реестр каналов: push (устройства), Telegram-бот, будущие каналы плагинов — со статусом
  «подключён / не подключён».
- Настройки: переключатель на канал + таблица «тип события × канал» для типов: таймеры и будильники /
  результаты задач / подтверждения / сообщения плагинов.
- Новый канал при подключении спрашивает «Присылать сюда уведомления Mia? Да / Нет» (в боте — кнопками);
  ответ записывается в настройки.
- Явный `delivery.channels` задачи по-прежнему главнее настроек; недоступный канал — `skipped` с причиной.
**DoD:** тесты маршрутизации (настройки × тип × явные каналы), бот спрашивает и включает канал, страница
«Уведомления» в Playwright; таймер у пользователя с включённым Telegram приходит в бот (фейковый Bot API).

#### M70 — Политика устройств и согласие на устройстве (backend)

**Objective:** ADR-0016 на сервере. **Preferred:** Opus 5.5 high. **Depends on:** — (main).
**Write scope:** `app/modules/tools/**` (policy, device channel), `app/modules/auth/web_handoff.py`
(привязка web-сессии WebView к устройству), `app/modules/notifications/**` (push с действиями),
`app/plugins/builtin/{alarm,telegram}/**` (SKILL/инструкции), `contracts/mvp/v1/*` (аддитивно), тесты.
**Work:** политика `allow|ask|deny` по (capability, requester, target, scope); запрос другого клиента →
подтверждение только на целевом устройстве (approvals M13 с каналом устройства), разовый скоуп на run,
постоянные гранты по requester с 30-дневным сроком и отзывом; `waiting_for_device` (2 мин) и понятный
отказ; push с действиями «Разрешить/Отклонить»; WebView-сессия = тот же device_id; минимизация
данных (имена и счётчики, без номеров); журнал обращений к устройству; `builtin.alarm` больше не ставит
системный будильник неявно, после первого облачного будильника — одно предложение «ещё в Часах».
**DoD:** web-запрос «найди Яну в контактах» ждёт подтверждения телефона (фейк-устройство в тестах),
отказ/таймаут/офлайн дают ясный ответ; WebView-запрос ведёт себя как с телефона; гранты истекают и
отзываются; ни одного номера телефона в событиях run.

#### M71 — Android: согласие, разрешения устройства, будильник Mia

**Objective:** ADR-0016 на телефоне. **Preferred:** Sonnet 5.5 high. **Depends on:** M70.
**Write scope:** A: `core/push/**` (действия push), `core/devicetools/**`, `feature/settings/**`,
`feature/web/**` (WebView только под `/mia/`, OI-M62-T1-001), новый модуль экрана согласия, тесты.
**Work:** экран/шторка согласия «<клиент> просит …» (разово / разрешать этому клиенту), настройки
возможностей и постоянных грантов с отзывом, журнал обращений; будильник Mia на весь экран со звуком
по push облачного будильника; WebView не держит ссылки других приложений домена.
**Также (после M77):** экран «Уведомления» в настройках приложения — те же каналы и таблица типов.
**DoD:** device gate: web-запрос → push → разрешить на телефоне → ответ в web; отклонить; постоянный
грант и отзыв; будильник Mia звонит при заблокированном экране.

#### M72 — Магазин: «Где работает» и возможности устройства

**Objective:** пользователь видит, что работает в облаке, а что на телефоне по запросу.
**Preferred:** Sonnet 5.5 medium. **Depends on:** M70 (поля манифеста).
**Write scope:** `contracts/mvp/v1/plugin-manifest.schema.json` (аддитивно), `app/plugins/builtin/*/mia.json`,
`app/modules/plugins/catalog/**` (отдача полей), `web/src/components/{PluginPage,StoreConsent}.tsx`, тесты.
**Также (OI-LIVE-002):** риск инструментов сервера по URL — из MCP-аннотаций (`readOnlyHint` → чтение без подтверждения, `destructiveHint` → `write_external`), с показом и правкой на экране согласия.
**DoD:** у будильника, Telegram, таймера блок «Где работает»; Linear по URL читает проекты без подтверждений; экран согласия перечисляет возможности
устройства; Playwright.

#### M73 — Поиск всегда есть, короткие ответы в чате, журнал инструментов

**Objective:** фактические вопросы отвечаются в чате с источниками; ошибки инструментов разбираемы.
**Preferred:** Sonnet 5.5 high. **Depends on:** — (main).
**Write scope:** `app/modules/runs/**` (промпты профилей, контекст даты/часового пояса пользователя,
события), `app/modules/jobs/tools.py` (описание `background.start`), `app/plugins/builtin/internet/**`,
`app/modules/plugins/composition.py` (встроенный поиск без установки), web/Android только отображение
причины ошибки шага — запросом к M75/M76 через handoff, тесты.
**Work:** «Интернет» (search/news/read) доступен всем без установки; модель отвечает коротко в чате,
`background.start` — только для долгого/отложенного/по просьбе, цель задачи — словами пользователя;
дата и часовой пояс пользователя в контексте; событие `tool.call.*` несёт аргументы (с редакцией
личных данных) и причину ошибки, `builtin.news.refresh` без результата сообщает запрос и причину.
**DoD:** «Подготовь краткую сводку главных новостей за сегодня» в чате → ответ со ссылками без задачи
(записанный ответ модели); журнал вызова содержит аргументы и причину ошибки.

#### M74 — Чаты и «Сейчас» (backend)

**Objective:** ADR-0017 на сервере + API активного. **Preferred:** Opus 5.5 high. **Depends on:** — (main).
**Write scope:** `app/modules/conversations/**`, `app/api/v1/{feed,conversations,now}.py`, свои миграции,
`app/modules/jobs/**` (карточка результата в чат-источник), `app/modules/notifications/telegram*`
(чат «Telegram», `/new`), `contracts/mvp/v1/*` (аддитивно), тесты.
**Work:** API чатов (список, создать, переименовать/автоназвание, закрепить, удалить), лента и
summary на чат, голос в любой чат, «продолжить, если прошло < 5 мин», хранение 60 дней без
активности (настройка 30/60/180/никогда, закреплённые не удаляются), результат задачи — в чат-источник
и во входящие; `GET /v1/now` — активные таймеры, будильники, фоновые задачи (из состояния плагинов и jobs).
**DoD:** два чата с разным контекстом (модель не видит соседний), задача из чата A приходит в A и во
входящие, retention удаляет только старые незакреплённые, `/v1/feed` без чата совместим.

#### M75 — Web: чаты и полоса «Сейчас»

**Preferred:** Sonnet 5.5 high. **Depends on:** M74 (и M73 для причины ошибки шага).
**Write scope:** `web/src/**` чат, список чатов, «Сейчас», настройки хранения; `web/tests/**`.
**Также:** Markdown-таблицы в ответах (OI-LIVE-001), причина ошибки шага (OI-LIVE-003).
**DoD:** Playwright: новый чат, переключение, переименование, закрепление; таблица в ответе; «Сейчас» с живым таймером и
отменой; экраны 390/768/1440.

#### M76 — Android: чаты, вызов кнопкой = новый диалог, «Сейчас»

**Preferred:** Sonnet 5.5 high. **Depends on:** M74.
**Write scope:** A: `feature/chat/**`, `feature/voice/**` (только выбор чата), `app/**` (навигация),
`feature/assistant/**`, тесты.
**Work:** список чатов и новый чат; боковая кнопка → новый диалог + «Продолжить прошлый» (< 5 мин);
полоса «Сейчас»; активный таймер в шторке уведомлений.
**Также:** Markdown-таблицы в ответах (OI-LIVE-001), причина ошибки шага (OI-LIVE-003).
**DoD:** device gate: вызов кнопкой создаёт новый чат, «Продолжить» возвращает прошлый; переключение
чатов; «Сейчас» отсчитывает.

#### Repair по живой проверке MW5b (добавлено 2026-10-11)

Пользователь проверил MW5b на https://mindmapgen.xyz/mia/ (web и APK `0.1.0 (05fff9d)`): бот с таймером — PASS;
остальное ниже. Общий контракт для истории шагов задаётся здесь, чтобы M74-R1, M75-R1 и M76-R1 шли параллельно:

`FeedItem.steps` (аддитивно, только у ответов ассистента, до 32 шт., в порядке вызова) — `FeedStep`:
`{tool_call_id, tool, plugin_id?, risk?, executor?, status, arguments?, error_code?, error_detail?, approval?}`;
`status` — `running | succeeded | failed | denied | expired | uncertain | cancelled`; `arguments` и `error_detail` — как в
`tool.call.started/completed` (M73, уже редактированы); `approval` — `{id, status: pending|approved|denied|expired|cancelled,
summary, decided_at?, channel?, device_id?}`. Пустой массив не отдаётся.

**M74-R1 — Фоновая задача закрывается своим результатом; история шагов и подтверждений (backend).**
Preferred: Opus 5.5 high. Depends on: main. Причина (2026-10-11): задача «отчёт по рынку электромобилей» прислала отчёт,
но осталась «в процессе»; затем пришло второе уведомление «Отчёт готов, уведомление отправлено», и только тогда задача
закрылась. Во «Входящих и задачах» отчёт не раскрывается. Шаги и подтверждения видны только в живом потоке (OI-M75-003),
поэтому подтверждение на телефоне не видно в web.
Write scope: `app/modules/jobs/**`, `app/modules/runs/**`, `app/modules/conversations/**`, `app/modules/notifications/**`
(только `notify` внутри задачи и содержимое inbox), `app/api/v1/{feed,inbox,jobs}.py`, `contracts/mvp/v1/*` (аддитивно),
своя миграция при необходимости, тесты.
Work: результат задачи — её итоговый ответ, ровно одна карточка и одно уведомление, статус `succeeded` в тот же момент;
`notify` внутри задачи не создаёт второй результат (или становится результатом, а финальная реплика модели не шлётся);
промпт задачи не просит «отправить уведомление». Запись inbox/задачи отдаёт полный текст результата (поле или
`GET`), клиент может его раскрыть. `FeedItem.steps` по контракту выше, строится из `run_events`/approvals при чтении ленты.
DoD: тест — задача с моделью, вызывающей `notify`, даёт одну карточку/одно уведомление и закрывается с ним; inbox отдаёт
полный текст; лента после перезагрузки содержит шаги с причиной ошибки и решённое подтверждение (решено «с телефона»).

**M72-R1 — Существующие установки по URL получают риск из аннотаций.** Preferred: Sonnet 5.5 high. Depends on: main.
Причина: Linear добавлен до M72; сохранённые риски (все `write_external`) считаются выбором пользователя, поэтому
«покажи проекты» по-прежнему просит подтверждения.
Write scope: `app/modules/plugins/catalog/**`, `app/modules/plugins/registry/**`, `app/modules/tools/{digest,mcp_client,policy}.py`
минимально, своя миграция, `web/src/components/StoreAddUrl.tsx` только если нужна пометка «изменено вами», тесты.
Work: различать «риск выбран пользователем» и «сохранён по умолчанию»; установки до M72 и неизменённые риски
пересчитываются из аннотаций сервера (при следующем обращении к инструментам или миграцией с повторным получением
списка); явный выбор пользователя по-прежнему главнее. Проверить, что Linear отдаёт `readOnlyHint` (запись в handoff).
DoD: тест — установка «как до M72» с 76 инструментами: чтение без подтверждения, создание задачи с подтверждением;
ручная правка пользователя сохраняется.

**M75-R1 — Web: компактные шаги, история, озвучка целиком, отчёт во «Входящих».** Preferred: Sonnet 5.5 high.
Depends on: контракт выше (параллельно с M74-R1, проверка вместе при интеграции).
Write scope: `web/src/**`, `web/tests/**` (включая `web/tests/support/e2e_server.py`).
Work: шаги инструментов, «Почему не получилось» и запросы подтверждения по умолчанию свёрнуты; старые сворачиваются в
горизонтальный ряд чипов, раскрытие по нажатию; переписка не уходит из виду из-за одного подтверждения (телефон 390 px);
после перезагрузки и с другого устройства шаги и решённые подтверждения берутся из `FeedItem.steps`; автоозвучка
после ответа читает то же, что кнопка «Озвучить» (сейчас — только первый абзац); во «Входящих и задачах» результат
задачи раскрывается полностью.
DoD: Playwright на 390/1440 — подтверждение свёрнуто в чип, лента видна; шаги из ленты после перезагрузки; автоозвучка =
«Озвучить» (перехват `speechSynthesis`); раскрытие отчёта во «Входящих».

**M76-R1 — Android: переключение чатов, компактные шаги, история, озвучка целиком.** Preferred: Sonnet 5.5 high.
Depends on: контракт выше. Причина: при выборе другого чата меняется заголовок, а лента — нет (и при повторных нажатиях).
Write scope: A `feature/chat/**`, `feature/voice/**` (только озвучка ответа в чате), `core/network/**` (аддитивно), тесты.
Work: переключение чата всегда показывает ленту выбранного чата (отмена/игнор устаревших загрузок); шаги и
подтверждения свёрнуты по умолчанию, старые — горизонтальный ряд чипов; история шагов из `FeedItem.steps`;
автоозвучка в чате читает то же, что «Озвучить».
DoD: JVM/Compose-тесты: быстрое переключение A→B→C показывает ленту C; свёрнутые шаги; шаги из ленты; текст автоозвучки;
`lint test assembleDebug`.

#### Repair по приёмке M60 (добавлено 2026-10-11)

Источник — [MVP_ACCEPTANCE.md](MVP_ACCEPTANCE.md). M41-R2 и M17-R1 готовы и не пересекаются (кроме одной строки `app/composition.py` у каждой — сливает integrator), их можно выполнять параллельно (`Выполни все готовые сессии wave MW6 параллельно`). M12-R1 ждёт решения пользователя по OI-M60-001; M22-R2 необязательна. После них — повтор M60 (перезапуск `tests/e2e_mvp`, strict xfail должны пройти) и живые UC-M60-*.

#### M41-R2 — Подтверждение называет получателя

- **Executor:** Opus 5.5 high (путь подтверждений — безопасность).
- **Objective:** текст подтверждения и голосовая фраза показывают имя человека (`Яна Петрова`), а не ссылку
  `person:<id>` / `contact:<n>`; аргументы и `args_hash` не меняются.
- **Write scope:** `app/modules/tools/{policy,router}.py` (резолвер имён для summary, привязка поздняя),
  `app/modules/memory/**` (имя по ссылке, только владельцу), одна строка в `app/composition.py`, `tests/tools/**`.
- **DoD:** strict xfail `tests/e2e_mvp/test_s03_yana.py::test_confirmation_names_the_recipient_chosen_by_ref` проходит
  (в summary «Яна Петрова», нет `person:`); тесты router и Telegram зелёные; web и Android показывают `summary` без правок.

#### M17-R1 — Удаление аккаунта и инкогнито для пользователя

- **Executor:** Opus 5.5 high (идентичность, удаление данных).
- **Objective:** пользователь может удалить аккаунт и начать инкогнито-разговор из web и Android.
- **Write scope:** backend `app/api/v1/account.py` (новый), `app/modules/users/**` или `app/modules/auth/**` (служба
  удаления), регистрация в `app/composition.py`, `contracts/mvp/v1/**` (путь и код ошибки), `tests/account/**`;
  web — «Ещё» («Удалить аккаунт» с подтверждением) и переключатель инкогнито в чате (`web/src/components/Chat.tsx`,
  `web/src/lib/chat.tsx`); Android — настройки «Удалить аккаунт», переключатель инкогнито в чате и голосе.
- **DoD:** `DELETE /v1/account` (bearer или cookie, Idempotency-Key, same-origin) выполняет `shred_account`, отзывает
  токены провайдеров (OI-M15-006), гранты и токены «Войти через Mia», сессии web и Android, push-токены, связь с
  Telegram, удаляет строку `users` (каскад); после этого старые bearer и cookie получают 401, новый вход Google создаёт
  пустой аккаунт; журнал доступа фиксирует удаление без содержимого; оба strict xfail в `tests/e2e_mvp/test_s07_memory.py`
  проходят; инкогнито-run из web и Android несёт `incognito: true` и показывает признак; Playwright и Android unit-тесты
  на новые элементы.

#### M12-R1 — Бюджет голосового хода (условно, после решения OI-M60-001)

- **Executor:** Sonnet 5.5 high.
- **Objective:** голосовой «Что у меня сегодня» читает 3–4 источника за один ход, не выходя из ~6 с до первого звука.
- **Write scope:** `app/core/config.py` (значение по умолчанию) и/или `app/modules/runs/engine.py` (параллельные
  read-вызовы одного шага считаются одним), `docs/adr/0011-*.md` (поправка), `tests/runs/**`.
- **DoD:** strict xfail `tests/e2e_mvp/test_s02_today.py::test_today_by_voice_reads_all_three_sources` проходит;
  существующие тесты лимитов голоса обновлены; запись (`write_*`) по-прежнему считается отдельно.

#### M22-R2 — Экспорт голосовых замеров (необязательно, не блокирует)

- **Executor:** Sonnet 5.5 high.
- **Objective:** экран голоса умеет скопировать/поделиться последними 20 `TurnMetrics` (первый звук, STT, старт run,
  TTS), чтобы UC-M60-8 не зависела от ручного переписывания.
- **Write scope:** Android `feature/voice/**`.
- **DoD:** unit-тест форматирования экспорта; кнопка в debug-сборке или по долгому нажатию.

### MW6b — Wake word и Personal Node (добавлено 2026-10-11)

Решения пользователя 2026-10-11: wake word и Personal Node входят в MVP отдельной волной MW6b **до** MW7 (M61, M62);
внешний вид не блокирует MVP (ревью дизайна — необязательные проверки). Wake word: **сначала сравнить движки**, фраза
**«Привет, Мия»** (OI-ARCH-003 → DECIDED). Аппаратный hotword (DSP), как у «Окей, Гугл», сторонним приложениям недоступен,
а саму фразу Google перехватить нельзя — поэтому детектор программный, на устройстве. Personal Node в первой версии должен
уметь всё четыре: локальные модели (Ollama), файлы и действия на ПК, хранение памяти и истории дома, режим «узел вместо
сервера». Поэтому сначала проектная сессия M85 (ADR + протокол), реализация — фазами M86–M90, карточки которых M85
уточняет (ниже — предварительные).

~~~text
MW6b: M80 (wake: движки) ◄─ M22        M81 (wake в фоне) ◄─ M80 + решение пользователя по ADR-0018
      M84 (экран «Устройства») ◄─ M70, M71, M72
      M85 (Node: ADR, протокол) ◄─ M60, M70
      M86 (Node: runtime, pairing, relay) ◄─ M85 + решение пользователя по ADR-0019
      M87 (файлы и действия на ПК) ◄─ M86     M88 (Ollama через узел) ◄─ M86     M91 (ядро на узле, TLS-туннель) ◄─ M86
      M89 (память и история дома) ◄─ M91     M90 (узел вместо сервера) ◄─ M89
      M92 (установщики) ◄─ M86     M93 (окно узла) ◄─ M92, M87, M88
MW7 ◄─ MW6b
~~~

| ID | Название | Зависимости | Репо | Preferred | Fallback |
|---|---|---|---|---|---|
| M80 | Wake word: сравнение движков | M22 | B, A | Opus 5.5 high | Sonnet 5.5 high |
| M81 | Wake word в фоне | M80, решение по ADR-0018 | A | Opus 5.5 high | Sonnet 5.5 high |
| M84 | Экран «Устройства» | M70, M71, M72 | B, A | Opus 5.5 high | Sonnet 5.5 high |
| M85 | Personal Node: ADR и протокол | M60, M70 | B | Opus 5.5 high | Opus 5.5 xhigh |
| M86 | Node: runtime, pairing, канал, «Устройства» | M85, M84, решения Р1–Р4, Р11 | B, A, N | Opus 5.5 high | — |
| M87 | Node: файлы и действия на ПК | M86 | B, N | Sonnet 5.5 high | Opus 5.5 high |
| M88 | Node: локальные модели (Ollama, LM Studio, OpenAI-совместимые) | M86 | B, N | Sonnet 5.5 high | Opus 5.5 high |
| M91 | Node: ядро на узле и TLS-туннель | M86, Р1–Р3 | B, N | Opus 5.5 high | — |
| M92 | Node: установщики Windows / macOS / Linux | M86 | B, N | Sonnet 5.5 high | Opus 5.5 high |
| M93 | Node: окно узла (данные, история запросов) | M92, M87, M88 | B, N | Sonnet 5.5 high | Opus 5.5 high |
| M89 | Node: память и история дома | M91 | B, N, A | Opus 5.5 high | — |
| M90 | Node: узел вместо сервера | M89 | B, N, A | Opus 5.5 high | — |

`N` — код узла в этом репозитории, `app/node/**` (решение M85).

#### M80 — Wake word: сравнение движков

**Objective:** выбрать детектор «Привет, Мия» для прослушивания в фоне по фактам, а не по описаниям.
**Read:** handoff S27, ADR-0005, ADR-0009, Review §2 (строка про DSP/VIS) и §6, Backlog §4, Android `core/wake/**`
(включая `evaluation/README.md`), handoff M20, M22.
**Write scope:** `docs/research/wake-word/**` (отчёт), `docs/adr/0018-wake-word-engine.md` (Proposed), Android
`core/wake/evaluation/**` и тесты `core/wake/src/test/**`. Продуктовый код и манифест не меняются. Аудио корпуса — вне git.
**Work:**
- кандидаты: openWakeWord (своя модель на синтетике русских TTS-голосов + негативы), Picovoice Porcupine (русская
  своя фраза; лицензия для личного использования и пилота 5–20 человек, ключ доступа, офлайн ли), keyword spotting
  sherpa-onnx (есть ли русская модель; open-vocabulary), базовая линия — спектральные шаблоны S27; при находке — другие
  открытые движки (microWakeWord и т. п.);
- по каждому: лицензия и цена, офлайн, интеграция в Android (AAR/ONNX/TFLite), размер модели, CPU на 1 с аудио
  (JVM/desktop-замер и оценка для ARM), стоимость обучения своей фразы;
- для 1–2 финалистов — прототип в harness `core/wake/evaluation`: синтетические позитивы (несколько голосов, скорость,
  шум) и негативы (русская речь с совместимой лицензией, музыка, ТВ): FRR и оценка ложных срабатываний в час;
- аккаунты в сторонних сервисах не регистрировать; если для Porcupine нужен ключ — отметить как решение пользователя.
**DoD:** отчёт с таблицей и измеренными числами; ADR-0018 Proposed с рекомендацией и вопросом пользователю (движок,
стоимость); уточнённая карточка M81 в handoff; предложенная проверка пользователя (запись своего голоса для дообучения,
если нужна).

#### M81 — Wake word в фоне (после M80 и решения пользователя)

**Objective:** «Привет, Мия» запускает голосовой режим и при выключенном экране — по желанию пользователя.
**Write scope (предварительно, уточняет M80):** Android `core/wake/**`, сервис ассистента и foreground service
микрофона (`app/src/main/**`, манифест), раздел настроек wake word, передача pre-roll в каскад M22.
**Work:** выключено по умолчанию; режимы «всегда» / «только на зарядке» / «с включённым экраном»; FGS типа `microphone`
от VoiceInteractionService (исключение Android 14+); пауза при звонке, другом приложении с микрофоном и батарее < 15 %;
до срабатывания звук не покидает телефон; срабатывание на заблокированном экране — по правилам M20; локальный счётчик
срабатываний и «это не мне».
**DoD:** unit/Robolectric-тесты; `lint test assembleDebug`; device gate → проверки пользователя: ложные срабатывания за
8 часов, пропуски на 50 произнесениях, батарея за ночь и за день.

#### M84 — Экран «Устройства» (web и Android)

Добавлено 2026-10-11 по просьбе пользователя: для «посмотри на телефоне» и подобных действий и для Personal Node нужен
один экран, где видно, какие устройства подключены, что на каждом может выполняться и в каком они состоянии.
**Objective:** раздел «Устройства» (в «Ещё» на web; в приложении — тот же экран через WebView M24 или нативно, решает сессия)
со списком устройств аккаунта и подробной карточкой каждого.
**Read:** ADR-0016, handoffs M70, M71, M72, M24, M77; `app/modules/tools/device_access.py`, `device_policy.py`;
контракт `/v1/devices/*` в `contracts/mvp/v1/openapi.yaml`.
**Write scope:** backend — модуль устройств рядом с `app/modules/tools/device_access.py` (или новый `app/modules/devices/**`),
`app/api/v1/**` (новые пути), миграция при необходимости, `contracts/mvp/v1/**` (аддитивно), `tests/devices/**`; web —
новый экран и пункт в «Ещё»; Android — отчёт о состоянии разрешений и «пульс», пункт настроек «Устройства».
**Work:**
- `GET /v1/devices` и `GET /v1/devices/{id}`: тип (телефон, браузер, позже — узел), имя, модель/ОС, когда подключено,
  `last_seen_at`; статус «онлайн» / «был(а) N мин назад» / «офлайн» / «нет push» (онлайн — открыт поток событий или
  контакт < 2 мин; узлы M86 — по WSS-соединению);
- что может выполняться на устройстве: возможности (включена ли в Mia, есть ли разрешение ОС), какие установленные
  плагины их используют («Где работает» M72), постоянные разрешения ADR-0016 с отзывом, ссылка на журнал доступа;
- переименовать устройство; «Отключить устройство» — выход этой сессии, удаление push-токена и разрешений;
- облачное выполнение показывается отдельной строкой «Облако Mia» (что работает без устройств);
- никаких номеров телефонов и содержимого — только имена возможностей и статусы.
**DoD:** API и экран проходят тесты (pytest, Playwright, Android unit); при двух телефонах и браузере видно три
устройства с верными статусами; отзыв постоянного разрешения и отключение устройства работают; M86 добавляет узлы в тот же
список (его DoD).

#### M85 — Personal Node: ADR и протокол

**Objective:** спроектировать Personal Node так, чтобы фазами получить все четыре возможности первой версии, не ломая
облачный режим и ADR-0016.
**Read:** `Mia_Architecture.md` §5–6, §11, §18; Review §7 (V1–V3, §7.5), §10, §12; ADR-0012, 0016, 0017; Backlog §2–3;
текущие `app/composition.py`, репозитории/модели БД (что завязано на Postgres), `app/modules/tools/device_access.py`,
`device_policy.py`, Model Gateway (`app/modules/models/**`), worker/jobs, handoffs M70, M71.
**Write scope:** `docs/adr/0019-personal-node.md` (Proposed), `docs/research/personal-node/**`, черновик
`contracts/node/v1/**` (pairing, конверт сообщения, RPC узла, манифест возможностей) — аддитивно, с проверкой валидатором.
**Work:**
- форма узла: то же Python-ядро как процесс `mia-node` (ПК/домашний сервер) — SQLite или встроенный Postgres;
  инвентаризация кода, завязанного на Postgres, и оценка цены;
- pairing по QR, ключи устройства, исходящий WSS к relay на сервере Mia, номера последовательности и защита от повтора;
  отзыв узла;
- шифрование клиент↔узел поверх relay — только проверенная библиотека/протокол (Noise, libsodium, MLS и т. п.), без
  своего криптопротокола; модель угроз (что видит relay, облачный провайдер модели, скомпрометированный web-клиент);
- маршрутизация: узел — ещё одно устройство в политике ADR-0016 (`allow | ask | deny`, `waiting_for_node`, без тихого
  отката в облако); где пользователь даёт согласие на действия на ПК;
- Ollama: адаптер Model Gateway через узел, потоковая выдача через relay, лимиты профилей ADR-0011;
- данные дома: что остаётся в облаке (метаданные, идентичность, push), что живёт на узле; поведение клиентов и фоновых
  задач при выключенном узле; синхронизация и бэкап;
- «узел вместо сервера»: вход без Google? (варианты), push, бот Telegram, доступ телефона вне дома (relay или
  туннель), перенос данных облако ↔ узел;
- фазировка и уточнённые карточки M86–M90 (write scope, зависимости, DoD, исполнитель).
**DoD:** ADR-0019 Proposed со списком решений для пользователя; черновик контрактов валиден; карточки M86–M90 в handoff
(integrator переносит в план).

#### M86–M91 — Personal Node, реализация (уточнено M85, 2026-10-11)

Источник — [handoff M85](handoffs/M85.md), [ADR-0019](../adr/0019-personal-node.md) (Proposed). M86 стартует после решений пользователя Р1–Р4, Р11 (OI-M85-001) и слияния M84.

Граф:

~~~text
M85 → [Р1–Р4, Р11] → M86 (+M84)
M86 → M87 ∥ M88 ∥ M91
M91 → M89 → M90
~~~

Параллельно идут M87, M88 и M91: у них разные write scope. Общие места:

- регистрация обработчиков RPC в `app/node/agent/handlers.py` — одна строка на сессию;
- `pyproject.toml`/`uv.lock` — зависимости собирает integrator.

`N` = этот репозиторий, `app/node/**`.

#### M86 — Node: runtime, pairing, управляющий канал, «Устройства» (B, A, N; Opus 5.5 high; L)

**Deps:** M85, M84 (слита), Р1–Р4, Р11.

**Write scope:**

- `app/node/{agent,keys,pairing,manifest,cli}/**` (новое: лёгкий режим `mia-node` без БД);
- `app/modules/nodes/**` (реестр, pairing, WS `/v1/nodes/connect`, presence, отзыв);
- `app/api/v1/nodes.py`;
- миграция (`nodes`; `devices.platform` = `node`);
- `contracts/node/v1/**`;
- `contracts/mvp/v1/{openapi.yaml,error-codes.json,README.md}` (аддитивно);
- модуль устройств M84: источник «узлы» (аддитивно);
- `pyproject.toml`/`uv.lock`: `[project.scripts] mia-node`, `keyring`;
- `tests/nodes/**`;
- web: «Устройства → Добавить компьютер» (код + сверка отпечатка), карточка узла;
- Android: сканирование QR (Google Code Scanner — без разрешения камеры), подтверждение отпечатка.

**Work:**

- ключ Ed25519 в хранилище ОС;
- pairing start/claim/poll по `pairing.schema.json`;
- handshake с одноразовым nonce;
- seq/ack/resume (буфер 256/60 с);
- кеш RPC-id;
- `manifest`;
- `node.status`, `node.unpair`, `node.rotate_key`;
- presence: онлайн по каналу, `last_seen_at` = момент закрытия;
- узел в `GET /v1/devices` (`NodeDeviceView`);
- переименование;
- «Отключить» = отзыв (кадр `revoked`, 4403, отказ в повторном входе, удаление грантов);
- автозапуск (launchd / задача Windows / systemd);
- путь job → узел через процесс API (OI-M85-004).

**DoD:**

- pytest (memory + Postgres):
  - pairing по QR и по коду;
  - несовпадение отпечатка;
  - истёкший код;
  - повтор `hello` отвергнут;
  - дубликат seq отброшен;
  - разрыв → 4400;
  - resume досылает кадры;
  - новая сессия переводит незавершённые вызовы в failed/uncertain;
  - отзыв закрывает канал и запрещает вход;
- e2e: настоящий WS-агент ↔ API в процессе;
- узел появляется в «Устройствах» онлайн, а после остановки офлайн (≤ 75 с);
- Playwright (добавить компьютер, отключить);
- Android unit + `lint test assembleDebug`;
- валидатор;
- проверка пользователя: поставить `mia-node` на свой ПК, отсканировать QR телефоном, увидеть ПК в «Устройствах»,
  отключить.

#### M87 — Node: файлы и действия на ПК (B, N; Sonnet 5.5 high, fallback Opus 5.5 high; M)

**Deps:** M86, Р5, Р12.

**Write scope:**

- `app/node/tools/**` (поиск и чтение: txt/md/csv/pdf/docx, `pypdf` и `python-docx` через integrator; `apps.open`,
  `links.open`; локальная граница `mia-node folders|apps`; журнал на узле);
- `app/modules/tools/{device_policy,device_access,device,router}.py` (доставка `node`, возможности, имена `node.*`,
  выбор узла при нескольких);
- `app/api/v1/approvals.py` (решает подтверждающий узла);
- `contracts/mvp/v1/device-access|device-tools` (аддитивно);
- Android: текст листа согласия для узла (`:feature:consent`, мелко);
- `tests/nodes/tools/**`.

**Work** — ADR-0019 §5:

- `file:`-ссылки на 24 ч, без абсолютных путей;
- запрет по умолчанию для `.env*`, `.ssh`, ключей, хранилищ паролей и профилей браузеров;
- проверка realpath и symlink;
- согласие на телефоне (`waiting_for: node`), гранты на run и на 30 дней;
- выключенный узел: ожидание ≤ 30 с для интерактивного run и ≤ 12 ч для задачи; без отката.

**DoD:**

- с телефона «найди договор аренды на компьютере и прочитай первый абзац» → согласие → ответ;
- «Отклонить» → ничего не прочитано;
- папка вне границы → `path_not_allowed`;
- тесты на traversal, symlink и denylist;
- узел выключен → `node_offline`;
- путей и содержимого нет в событиях и логах;
- журнал на узле и в access log.

#### M88 — Node: локальные модели (Ollama, LM Studio, OpenAI-совместимые) (B, N; Sonnet 5.5 high, fallback Opus 5.5 high; M)

Решение пользователя 2026-10-11: кроме Ollama — как минимум LM Studio. Мост узла работает с «бэкендами локальных моделей»:
`ollama` (родной API), `lmstudio` (OpenAI-совместимый сервер LM Studio, по умолчанию `localhost:1234/v1`) и `openai_compat`
(любой локальный OpenAI-совместимый сервер: llama.cpp server, Jan, LocalAI, vLLM — адрес задаёт пользователь на ПК).
Узел сам находит запущенные Ollama/LM Studio, список моделей объединяется с пометкой источника; поддержка tools
определяется по модели (без tools — только чат). Ниже `ollama` читать как «любой из этих бэкендов».

**Deps:** M86.

**Write scope:**

- `app/node/models/**` (мост к Ollama: `model.list|warmup|generate`, отмена, одна генерация);
- `app/modules/models/adapters/{node_ollama.py,__init__.py}`;
- `catalog.py` (`ollama` → chat_completions с tools; провайдер `node-ollama` без URL);
- `connections.py` (не в цепочке `mia:default`; явный флаг облачного резерва);
- `app/modules/runs/{profiles,engine}.py` (`max_output_tokens` по профилю, OI-M85-005);
- web «Модели» (модели ПК) и экран «Где ваши данные» (честный текст про режим `cloud`);
- `tests/models/node/**` со сценарным Ollama.

**DoD:**

- чат и голос отвечают моделью ПК (e2e со сценарным Ollama);
- дельты приходят в SSE до конца ответа;
- вызов инструмента (таймер) через модель ПК;
- отмена останавливает генерацию;
- warmup при открытии голоса;
- ПК выключен → понятная ошибка, без облака;
- проверка пользователя (OI-M85-008): 2–3 модели на своём ПК, сценарии M60 №1, 2, 4 и задержка первого звука.

#### M91 — Node: ядро на узле и сквозной TLS-туннель (B, N; Opus 5.5 high; L) — новая

**Deps:** M86, Р1–Р3. Инфраструктура relay — OI-M85-002.

**Write scope:**

- `app/node/runtime/**` (встроенный Postgres 16: загрузка с SHA-256, `initdb`, запуск и остановка; `mia-node serve`
  = API + циклы воркера в lifespan);
- `app/core/{redis,config,runtime}.py` (in-process реализация 4 методов, `REDIS_URL` необязателен, `MIA_ROLE=node`
  — минимально);
- `app/node/tunnel/**` (мультиплексор, TLS на узле → ASGI);
- `app/relay/**` + `deploy/relay/**` (сервис `mia-relay`: SNI, туннели, грант по токену);
- `app/modules/nodes/{relay_grant,certs}.py` (ACME DNS-01);
- `tests/nodes/runtime/**`, `tests/relay/**`.

**DoD:**

- `mia-node serve` на Linux и macOS (Windows — проверка пользователя) поднимает Postgres, миграции и полный API;
- подмножество e2e M60 проходит на профиле узла;
- Playwright через relay в процессе с тестовым CA (trustme): буферы relay не содержат маркер открытого текста;
- тест pin в OkHttp;
- выпуск сертификата против Pebble или мока;
- переподключение туннеля.

#### M92 — Node: установщики для Windows, macOS и Linux (B, N; Sonnet 5.5 high, fallback Opus 5.5 high; M)

Добавлено 2026-10-11: пользователь выбрал установщики (Р11 = б).
**Deps:** M86 (облегчённый режим без БД); после M91 установщик дополняется встроенным Postgres (DoD M91).
**Write scope:** `packaging/node/**` (сборка, иконки, скрипты), `.github/workflows/node-release.yml` (сборка на трёх ОС),
`app/node/cli/**` (команды `install-service`/`uninstall`), `docs/NODE_INSTALL.md`; без правок ядра.
**Work:**
- выбрать способ упаковки Python-ядра (Briefcase, PyInstaller/Nuitka + нативные установщики и т. п.) — по размеру,
  обновлениям, поддержке трёх ОС;
- macOS: `.dmg`/`.pkg` (Apple Silicon и Intel), автозапуск launchd, ключ в Связке ключей;
- Windows: `.msi` или `.exe`, служба или задача автозапуска, ключ в Диспетчере учётных данных;
- Linux: `.deb` и `.rpm` (или AppImage), systemd-unit; Docker-образ для домашних серверов;
- значок в трее / строке меню: статус (онлайн / нет связи / на паузе), «Привязать к Mia» (показывает QR и код),
  «Пауза», «Выход», «Открыть журнал»;
- обновления: проверка версии на сервере Mia, загрузка подписанного пакета;
- подпись кода: собрать без подписи с понятной инструкцией обхода предупреждения, подпись включается переменными CI, когда
  пользователь решит про сертификаты (OI-M85-011).
**DoD:** установщики собираются в CI на трёх ОС; установка → автозапуск → привязка по QR → ПК в «Устройствах» на macOS
(проверено агентом) и в CI-смоуке Linux/Windows; удаление чистое; проверка пользователя на его ПК.

#### M93 — Node: окно узла — данные и история запросов (B, N; Sonnet 5.5 high, fallback Opus 5.5 high; M)

Добавлено 2026-10-11 по желанию пользователя (не в первой версии узла).
**Deps:** M92, M87, M88; раздел данных — после M89.
**Write scope:** `app/node/panel/**` (локальная страница на `127.0.0.1`, вход только с этого ПК), `web/` (общие компоненты —
аддитивно), тесты.
**Work:** окно открывается из трея; разделы: «Состояние» (связь, версия, ключ/отпечаток, кто привязан); «Модели»
(найденные Ollama / LM Studio / свои адреса, проверка); «Доступ» (разрешённые папки и программы — меняются только здесь);
«История запросов» (что Mia просила у этого ПК: когда, откуда, инструмент, решение, без содержимого файлов по умолчанию);
«Данные» (после M89: что хранится дома, размер, бэкап, экспорт, удаление); стиль M33.
**DoD:** страница доступна только с ПК (проверка Origin и токен сессии), Playwright-тесты разделов; проверка пользователя.

#### M89 — Node: память и история дома (B, N, A; Opus 5.5 high; L)

**Deps:** M91, Р6, Р9.

**Write scope:**

- `app/modules/nodes/home/**` (`data_home` аккаунта, приглашение устройства, переключение);
- порт `IdentityVerifier` в `app/modules/auth/**` (аддитивно);
- `app/modules/oauth/**` (узел как клиент, аддитивно);
- `app/node/{identity,telegram}/**` (свой бот, `getUpdates`);
- канал push через `push.send` (`notifications/channels.py`, аддитивно);
- `oauth.callback`;
- импорт памяти M17 на узел;
- web: переключатель «Данные дома», переход на адрес узла, «Компьютер не в сети», «Временный чат в облаке»;
- Android: базовый URL узла + pin + баннер офлайна;
- `tests/nodes/home/**`.

**DoD:**

- сквозные M60 №5 и №7 в режиме `home`;
- после переключения в облачных таблицах нет строк содержимого аккаунта (тест);
- узел выключен → `node_data_unavailable`, без отката;
- push без содержимого будит телефон;
- relay не видит открытого текста;
- проверка пользователя: телефон вне дома (мобильная сеть) читает чаты с ПК.

#### M90 — Node: узел вместо сервера (B, N, A; Opus 5.5 high; L)

**Deps:** M89, Р7, Р8, Р10.

**Write scope:**

- `app/node/standalone/**` (локальный владелец, passkeys — библиотека `webauthn`, вход ключом устройства);
- `app/modules/archive/**` (`mia-archive/v1`: экспорт, импорт, заморозка);
- `/v1/account/archive`;
- `app/node/backup/**` (age через `pyrage`, `backup verify`);
- web: вход passkey на узле, мастер переноса;
- Android: вход в узел по QR без облака;
- `docs/NODE_INSTALL.md`;
- `tests/nodes/standalone/**`.

**DoD:**

- e2e M60 на узле без облачной идентичности;
- перенос облако → узел → облако: счётчики и хеши совпадают, облачное содержимое уничтожено (DEK);
- бэкап и проверка восстановления;
- инструкция установки;
- проверки пользователя: установка по инструкции, телефон вне дома, свой бот Telegram, восстановление из бэкапа.

## 7. Wave gates

| Gate | Условия |
|---|---|
| **MW0** | ADR 0010–0015 приняты; `contracts/mvp/v1` валидны; основная модель выбрана |
| **MW1** | Миграции M10, M11, M12 в одной линейной цепочке; один аккаунт на web и Android; run с основной моделью без tools отвечает через SSE; Android запускается как ассистент; STT измерен |
| **MW2** | Таймер вызывается моделью через MCP и router; подтверждения работают; голосовой каскад отвечает на телефоне |
| **MW3** | Плагин проходит путь публикация → установка → OAuth; расписание переживает рестарт; «Запомни, что Яна…» работает; device tools на телефоне; `GET /v1/feed` отдаёт голос и чат одной лентой; токены M33 генерируются для web и Android, style-страница готова к ревью |
| **MW4** | Web на новом API; Linear и Telegram в магазине; OpenAI SDK работает через Mia; чат на web и Android с голосовым вводом и озвучкой, в стиле M33 |
| **MW5** | Дашборд MCP Apps; Android WebView и push; MindWeaver через Mia; `web_poc` удалён |
| **MW5b** | ADR-0016 и ADR-0017 работают сквозь web и телефон: согласие на устройстве, «Где работает», встроенный поиск, журнал инструментов, чаты, «Сейчас» |
| **MW6** | `MVP_ACCEPTANCE.md`: READY, или NOT READY со списком repair |
| **MW6b** | «Привет, Мия» срабатывает в фоне по желанию, замеры ложных срабатываний и батареи приняты пользователем; Personal Node: pairing, файлы/действия на ПК, Ollama, память дома и режим «узел вместо сервера» проходят сквозные тесты |
| **MW7** | Вторая модель проверена; пилотный деплой с восстановлением |

## 8. OPEN_ISSUES блоки

Каждая M-сессия пишет только в свой блок `## Mxx inbox` в
[OPEN_ISSUES.md](OPEN_ISSUES.md). ID записей: `OI-Mxx-NNN`.

Существующие вопросы и сессии, которые их закрывают:

| Вопрос | Закрывающая сессия |
|---|---|
| OI-W6-013 | M11 |
| OI-WEB-001 | M10, M11 |
| OI-WEB-002 | M16 |
| OI-WEB-003 | Бэклог (открытая публикация) |
| OI-WEB-004 | M01, M61 |
| OI-WEB-005 | M10, M50 |
| OI-W6-005, OI-W6-017 | M22 |
