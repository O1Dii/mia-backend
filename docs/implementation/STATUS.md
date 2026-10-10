# MIA implementation status

Обновляется wave integrator после проверки handoff и commits. Worker sessions не объявляют downstream session READY самостоятельно.

Допустимые состояния: PENDING, READY, IN_PROGRESS, COMPLETED, PARTIAL, BLOCKED, FAILED, SUPERSEDED.

## MVP M-plan — 2026-10-09

Текущий план: [MVP_SESSION_PLAN.md](MVP_SESSION_PLAN.md); архитектура —
[Mia_Architecture_Review.md](../Mia_Architecture_Review.md). Старые S28, S31-R1 и
S33 заменены (SUPERSEDED) сессиями M20, M22 и M60; S27 остаётся DEFERRED.
**MW0 integrated 2026-10-09** (integrator: Opus 5.5 high, вместо Sonnet 5.5 high по правилу «более сильная модель»).
Merge: M00 → `3f904b6`, M01 → `f52cc2a`, integration commit поверх них. Gate MW0 PASS:
ADR-0010…0015 Accepted (ADR-0014 заполнен решением M01); `validate_contracts.py` PASS
(mvp/v1: 24 examples, 13 run events, 48 operations); pytest 222 passed / 63 skipped;
spike tests 13 passed; mypy (136 files) и `mypy --strict` ports/fakes PASS; ruff PASS.
Основная модель: **GPT на плане ChatGPT через Sign in with ChatGPT**; резерв — DeepInfra
GLM-5.3-Flash. Применены shared-file запросы M00: `.python-version` = 3.13, `pyyaml`/`referencing`
в dev-группе, `PyYAML` в `contracts/requirements.txt`.
MW0: готовы были M10, M11, M12, M20, M21 (волна MW1, параллельно; не больше пяти активных, по умолчанию четыре).

**MW1 integrated 2026-10-09** (integrator: Opus 5.5 high). Backend merge: M10 → `839200e`, M11 → `e12ce12`,
M12 → `e4f0cc6` (конфликты `.env.example`/`config.py`/`migrations/env.py` — аддитивные блоки, обе стороны),
coordination M20 → `6028f42`, M21 → `8c037e6`, integration commit поверх. Android main: M20 → `34e978b`,
M21 → `522c517`, без конфликтов. Integrator: миграции в линейную цепочку
`20260913_0003 → 20261009_m10_0001 → 20261009_m11_models → 20261009_m12_runs` (head); шлюз M11 передан в
`install_runs(model_gateway=...)`; `model_failover` добавлен в `KNOWN_EVENTS` (запрос M11); тест
`tests/integration/test_mw1_gate.py`.

Gate MW1 — PASS (живой вызов основной модели подтверждён пользователем 2026-10-09, см. ниже):
- миграции M10–M12 в одной цепочке: PASS (upgrade → downgrade `20260913_0003` → upgrade, `alembic check` без drift);
- один аккаунт на web и Android: PASS (M10: cookie и bearer → один `user_id` по Google `sub`);
- run с основной моделью без tools отвечает через SSE: PASS офлайн (реальный `DefaultModelGateway`, `chatgpt-plan`,
  записанный Responses-ответ → `run.started … response.final … run.completed`). **Живой прогон не выполнен**: нужен
  интерактивный вход владельца, `uv run python -m app.modules.models.smoke --provider gpt-plan` (handoff M11);
  **живой smoke M11 выполнен пользователем 2026-10-09: PASS** — Sign in with ChatGPT, подпись ID-токена проверена по
  JWKS, `chatgpt-plan/gpt-6.1-sol`, tool-цикл 3 хода (`get_time`, `add_todo`), first event 4112 мс (один замер;
  M01 p50 1340 мс). Это прямой вызов адаптера; путь run → SSE проверен офлайн тестом выше;
- Android запускается как ассистент: PASS (device gate M20 на SM-S938B, разблокированный и заблокированный экран);
- STT измерен: PASS (M21, SM-S938B: p50 106 мс на 3–5 с речи, PSS 286 МБ, WER 6.9 % на синтезе).

Проверки: ruff, `ruff format --check`, mypy (223 файла), `validate_contracts.py` (mvp/v1: 14 run events, 18 error codes)
PASS; pytest с PostgreSQL+Redis 552 passed / 4 skipped (credential smokes), без БД 375 passed / 181 skipped.
Android main (JDK 17): `lint test assembleDebug` + androidTest-сборки `:feature:assistant`, `:core:stt` — BUILD
SUCCESSFUL, 150 unit-тестов, 0 failed.

Follow-ups MW1 (не блокируют MW2):
- `/v1/runs*` и `/v1/model-connections*` принимают только bearer; cookie-principal M10 (`request_authenticator`) нужен
  до перевода web на новый API — M30 (или M13, если трогает composition).
- Запрошенные аддитивные правки `contracts/mvp/v1/openapi.yaml` не применены: M10 (`cookieAuth`, пути workspace/
  installations/dashboard, 409 `version_conflict`), M11 (`/v1/model-connections*`). Владелец — следующая сессия,
  меняющая контракты (M13/M14), или отдельный follow-up.
- Реестр `error-codes.json` пока без кодов M10 (`version_conflict`, `tools_digest_mismatch`, …) и API M11.
- Android: два fixture-теста `:core:network` ищут `../mia-helper/contracts`; из sibling worktree нужен
  `-PmiaContractsDir=<backend>/contracts/realtime/v1/fixtures`.
- Ручная проверка M20 пользователем 2026-10-09: удержание боковой кнопки на SM-S938B с назначенной Mia открывает
  Mia — PASS. Баннер роли после разблокировки и запрос разблокировки при уходе с голосового экрана не подтверждались
  отдельно (покрыты JVM-тестами).
- OI-M21-002 DECIDED (пользователь): M22 предлагает скачать модель STT (~225 МБ) по согласию в любой сети, без
  требования Wi-Fi.

MW2 стартовала 2026-10-09: M13, M14, M22 параллельно (worktrees `../mia-helper-wt/M13`, `M14`, `M22-coordination`,
`../mia-helper-android-wt/M22`). Диспетчерские правила волны: `mcp==2.2.0` в обеих backend-сессиях; встроенные плагины в
`app/plugins/builtin/<id>/` (контракт M00 требует `server.module` под `app.`); router M13 не импортирует M14 — получает фабрику;
идентичность вызова в `_meta` (`mia/user_id`, `mia/workspace_id`, `mia/installation_id`, `mia/run_id`).

**MW2 integrated 2026-10-09** (integrator: Opus 5.5 high). Backend merge: M13 → `6dc045a`, M14 → `b6f2563` (конфликты
`pyproject.toml`/`uv.lock`/`tests/hardening/test_admission_and_resilience.py` — взята версия M13, lock перегенерирован
`uv lock`), coordination M22 → `216a7c7`, integration commit поверх. Android main: M22 → `1cc92e8`, без конфликтов.
Миграции: одна новая `20261009_m13_tools` (down_revision `20261009_m12_runs`), head; у M14 миграций нет.
Integrator:
- wiring `app/modules/plugins/composition.py`: `RouterServerFactory` (фабрика M14 → сигнатура router M13),
  `BuiltinPluginCatalog` (digest = `compute_tools_digest` по `tools/list` того же сервера, OI-M13-004),
  `GatewayHostLlm` (модель пользователя для `builtin.news`, profile `job`); `app/composition.py`: каталог → `install_workspace`,
  фабрика → `install_tools` (workspace теперь монтируется после `install_models`);
- OI-M13-001: обратимые алиасы имён tools в адаптерах M11 (`adapters/tool_names.py`, `responses.py`, `chat_completions.py`);
- OI-M22-002: движок не выпускает `speech.ack` после `approval.requested`;
- запрос M14: пример `contracts/mvp/v1/examples/plugin-manifest/timer.mia.json` → `app.plugins.builtin.timer.server`;
- gate-тест `tests/integration/test_mw2_gate.py`.

Gate MW2 — PASS офлайн; живые проверки — пользователь (см. ниже):
- таймер вызывается моделью через MCP и router: PASS офлайн — вход Android по bearer → `POST /v1/installations`
  `builtin.timer` с digest каталога → `POST /v1/runs` (voice) → реальный `DefaultModelGateway` `chatgpt-plan` (записанный
  Responses-поток вызывает алиас `builtin__timer__start`) → router M13 → in-process MCP-сервер M14 → `tool.call.completed`
  (`structured_content.items`, `ui://`) → `response.final`; таймер лежит в `PluginState` (`GET …/state`). Живой вызов основной
  модели с tools не выполнялся (нужен интерактивный вход владельца);
- подтверждения работают: PASS — `write_external` → `approval.requested` → `GET /v1/approvals?status=pending` →
  `POST /v1/approvals/{id}/decision` (неверный `args_hash` → 409 `approval_args_mismatch`, верный → 200, channel `android`)
  → `approval.resolved` → `tool.call.completed{succeeded}`, Postgres;
- голосовой каскад отвечает на телефоне: PASS в M22 на SM-S938B (реальные GigaAM STT, Google TTS, SSE-клиент, M10+M12 на
  Postgres; модель и M13 — фейки, речь инжектирована): 20 ходов, конец речи → первый звук p50 804 / p95 836 мс, barge-in
  stop 85–88 мс + cancel 89 мс, reconnect по `Last-Event-ID`, подтверждение голосом (approve/deny/expired).

Проверки: ruff, `ruff format --check`, mypy (292 файла), `validate_contracts.py` (mvp/v1: 24 examples, 14 run events,
49 operations, 41 error codes) PASS; pytest с PostgreSQL+Redis 804 passed / 4 skipped, без БД 611 passed / 197 skipped;
alembic upgrade → downgrade `20261009_m12_runs` → upgrade, `alembic check` без drift. Android main (JDK 17): `lint test
assembleDebug` + androidTest-сборки `:feature:voice`, `:feature:approvals`, `:core:tts`, `:core:network`, `:feature:assistant`,
`:core:stt` — BUILD SUCCESSFUL, 204 unit-теста, 0 failed.

User checks MW2 (нужен человек):
1. Живой путь: вход Google в приложении, подключение основной модели (Sign in with ChatGPT на хосте backend), согласие на
   загрузку STT (225 МБ), несколько вопросов голосом, в т.ч. «поставь таймер на пять минут» (живой вызов tool с алиасом),
   замер первого звука с реальной моделью. (Проверка Gemini Live снята: режим скрыт и вне MVP, см. ниже.)
2. Реальный микрофон и динамик: barge-in и самоперебивание эхом TTS (OI-M22-001).
3. Подтверждение на заблокированном экране: «Одобрить» сначала просит разблокировку.
4. WER на живом голосе (OI-M21-004).

Follow-ups MW2 (не блокируют MW3):
- Запрошенные аддитивные правки `contracts/mvp/v1` не применены: M14 — `get_state` у декларативных (OI-M14-001) и
  `permissions.publicWeb` (OI-M14-002); M10/M11 — пути и `cookieAuth` в `openapi.yaml` (с MW1). Владелец — M15.
- Grants не проверяются (OI-M13-002) — словарь определяет M15.
- Устройства: `PUT /v1/devices/{id}/tools` и `POST /v1/runs/{id}/device-tool-results` — M23 (канал M13 и клиент M22 готовы).
- `GatewayHostLlm` без бюджета на установку (OI-M14-005).

Ручные проверки пользователя MW2 перенесены в [USER_CHECKS.md](USER_CHECKS.md) (UC-MW2-1…5, UC-MW1-1).

**Изменение scope 2026-10-09 (после MW2, решение пользователя):** голосовой режим M22 остаётся отдельной функцией; обычный
чат на web и Android получает голосовой ввод по кнопке и озвучку ответов; web и Android — общая дизайн-система
(иммерсивный объёмный отзывчивый стиль). Добавлены M18 (лента, API, MW3), M33 (дизайн-система web + Android, MW3), M25
(Android: чат на runs, голос, UI, MW4); M30 расширен голосом и дизайном, M60 зависит от M25. Карточки — в
[MVP_SESSION_PLAN.md](MVP_SESSION_PLAN.md).

**Gemini Live вне MVP (решение пользователя 2026-10-09):** код сохранён, режим скрыт — Android-флаг
`LIVE_VOICE_ENABLED = false` (пункт в настройках не показывается, сохранённый выбор Live → каскад), Android main `ec4fb68` (`lint test assembleDebug` PASS). Возврат — `Mia_Backlog.md` §4.

MW3 стартовала 2026-10-09: M15, M16, M17, M23, M33 параллельно, M18 — в освободившийся слот после M23 (worktrees
`../mia-helper-wt/<Mxx>`, Android `../mia-helper-android-wt/{M23,M33}`). Диспетчерские правила волны: миграции от
`20261009_m13_tools`; backend-эндпоинты device tools — M23 в `app/api/v1/device_tools.py` (push-token M16 — `devices.py`);
M15 — владелец отложенных правок `contracts/mvp/v1` (M10/M11 пути, `cookieAuth`, OI-M14-001/002); M16 — точечные хуки в
builtin timer/alarm (не понадобились).

**MW3 integrated 2026-10-09** (integrator: Opus 5.5 high вместо Sonnet 5.5 high по правилу «более сильная модель»). Backend
merge: M18 → `9079c21`, M15 → `5f3a245`, M16 → `efe55c3`, M17 → `6d3a44c`, M23 → `8cb886d`, M33 → `294b1d9`, integration
commit поверх. Android main: M23 → `1450389`, M33 → `3565859`, без конфликтов. Конфликты только ожидаемые: `error-codes.json`
(объединение по `code`, 73 кода), `.env.example`, `migrations/env.py`, `app/composition.py` (порядок M15
`build_catalog_stack → install_workspace → build_connections → install_tools → install_store`, затем `install_memory` до
`install_runs`; после `bind_publisher`: jobs, feed, memory publisher, device tools), `core_tools.py` (пространства
`background`, `memory`, `people`, `schedule`, `skills` + `notify`), `tests/tools/test_wiring.py`.
Integrator:
- миграции в линейную цепочку `20261009_m13_tools → 20261009_m15_store → 20261009_m16_jobs → 20261009_m17_memory` (head);
- OI-M17-003: Android M23 отдаёт `{"matches": [{name, phone_count}]}` без номеров — `parse_contacts` (M17) читает `matches`;
  без этого `people.resolve` никогда не находил контакты телефона;
- OI-M18-002: `cookieAuth` уже глобальный (M15) — `/v1/feed` наследует;
- gate-тест `tests/integration/test_mw3_gate.py`: все поверхности MW3 в продакшн-приложении, инструменты M16+M17 в одном
  реестре, `people.resolve` на ответе телефона в формате M23 (Postgres).

Gate MW3:
- плагин проходит путь публикация → установка → OAuth: PASS офлайн (M15: 6 builtin публикуются; add-by-URL с тестовым
  MCP-сервером и тестовым AS — PKCE S256, resource indicator, PRM, DCR, refresh, отзыв RFC 7009; повторное согласие);
- расписание переживает рестарт: PASS (M16: настоящий процесс `python -m app.worker`, SIGTERM, пропущенный запуск по
  политике после рестарта; двойной claim невозможен — 8 воркеров × 60 jobs);
- «Запомни, что Яна…» работает: PASS офлайн через движок на Postgres (M17); живой голосом — UC-MW3-3;
- device tools на телефоне: PASS (2026-10-09, после интеграции, SM-S938B, Android 16, разблокирован): `AndroidDeviceToolsTest`
  5/5 (окно READ_CONTACTS: отказ → структурная ошибка, разрешение → контакт найден; будильник; Настройки; Telegram),
  `DeviceToolsVoiceGateTest` 4/4 + запрос разрешения в голосовом ходе 1/1 (речь → STT → backend → tool → TTS). Integrator
  исправил обвязку gate (Android `f4be59b`): тестовый контакт пишется под shell-идентичностью — выданный приложению
  WRITE_CONTACTS заставлял Android 16 авто-отклонять окно READ_CONTACTS; повтор после `pm revoke` убран (Android выдаёт
  разрешение без окна); скриншоты gate в `.gitignore` (личные чаты Telegram). Контрольный прогон исправленного скрипта
  прерван блокировкой экрана — изменение скрипта только убирает повтор;
- `GET /v1/feed` отдаёт голос и чат одной лентой: PASS (M18; bearer и cookie; keyset без пропусков при параллельной записи;
  исправлен deadlock `open_turn`, OI-M18-001);
- токены M33 генерируются для web и Android, style-страница готова к ревью: PASS (drift-тест, контраст ≥ 4.5:1) → UC-MW3-1.

Проверки: ruff, `ruff format --check`, mypy (395 файлов), `validate_contracts.py` (mvp/v1: 25 examples, 14 run events,
87 operations, 73 error codes) PASS; pytest с PostgreSQL+Redis 1115 passed / 5 skipped / 0 failed (первый прогон под нагрузкой Gradle: 1 flaky `tests/jobs/test_process.py` — SIGTERM до установки обработчика, отдельно 3×PASS), `tests/design` с `MIA_ANDROID_DIR` 30 passed, gate MW3 2 passed; alembic upgrade → downgrade `20261009_m13_tools` → upgrade, `alembic check`
без drift. Android main (JDK 17): `lint test assembleDebug` + androidTest-сборки `:core:devicetools`, `:feature:voice` —
BUILD SUCCESSFUL, 250 unit-тестов, 0 failed.

User checks MW3: UC-MW3-1 ревью дизайна — **PASS** (пользователь 2026-10-09: «дизайн хороший»); UC-MW3-2 device gate M23 —
PASS (выше); открыта UC-MW3-3 (Яна живым голосом) — [USER_CHECKS.md](USER_CHECKS.md).

Follow-ups MW3 (не блокируют MW4):
- роутер не сверяет grants (OI-M15-001, нужен backfill установок) и показывает модели app-only `get_state` (OI-M15-005);
- OAuth `state` не привязан к браузеру (OI-M15-003) — закрыть в M30 вместе с UI подключения;
- пример `contracts/mvp/v1/examples/device-tools/registration.json` всё ещё называет `telegram.share`, реализован
  `telegram.handoff` (карточка M23) — переименовать при следующей правке контрактов;
- нет публичного удаления аккаунта (OI-M17-004) и отзыва токенов провайдеров при нём (OI-M15-006) — M62.

**Gate MW3 — PASS** (2026-10-09). Готовы к старту (волна MW4): M30, M25, M40, M41, M50, M42 (optional).

MW4 стартовала 2026-10-09: M30, M25, M40, M41, M50 параллельно (пять слотов), M42 (optional) — в освободившийся слот
(worktrees `../mia-helper-wt/<Mxx>`, coordination M25 — `../mia-helper-wt/M25-coordination`, Android
`../mia-helper-android-wt/M25`). Диспетчерские правила волны:
- миграции от `20261009_m17_memory` (M41, M50 — свои revision-файлы; integrator выстроит цепочку);
- у каждой backend-сессии своя одноразовая PostgreSQL (`postgres:16-alpine`, порты M30 55430, M40 55440, M41 55441,
  M42 55442, M50 55450) и свой номер БД Redis (`redis://localhost:6379/<N>`, N = 30/40/41/42/50); общая тестовая БД не
  используется — фикстуры делают `TRUNCATE users CASCADE`;
- `web/vite.config.ts`, `web/playwright.config.ts`, `web/package.json` — M30; M50 добавляет только свой entry
  (`web/oauth.html` + один пункт `build.rollupOptions.input`) и тесты в `web/tests/oauth/**`;
- встроенный серверный код Telegram — `app/plugins/builtin/telegram/**` (контракт M00: `server.module` под `app.`),
  webhook бота — `app/api/v1/telegram.py`; регистрация в `app/composition.py` и `app/modules/notifications/` — только
  добавление строк;
- `contracts/mvp/v1/*` и `error-codes.json` — только аддитивно, с записью в handoff;
- телефон SM-S938B (`R5CY12AMHLY`) — общий ресурс M25 и M41: device gate только под lock-каталогом
  (`mkdir` атомарен), `adb reverse` — на свой порт (M25 8125, M41 8141), после прогона lock снимается;
- реальные сообщения людям не отправляются; живые OAuth/голос/бот-токен — в handoff как кандидаты в `USER_CHECKS.md`.
- поправка диспетчера: в Redis только DB 0–15, назначенные номера 30–50 недопустимы — сессии подняли свои контейнеры Redis.

**MW4 integrated 2026-10-09** (integrator: Opus 5.5 high вместо Sonnet 5.5 high по правилу «более сильная модель»). Backend
merge: M40 → `9a91960`, M42 → `cd6762f`, M30 → `e86bc7b`, M41 → `30ceac5`, M50 → `293fc8f`, coordination M25 → `8d27ab0`,
integration commit поверх. Android main: M25 → `fbc3e1a`, без конфликтов. Конфликты только ожидаемые (M50 против M30/M41):
`app/core/config.py` (`TELEGRAM_*` + `OAUTH_*`/`APPS_*`), `migrations/env.py`, `error-codes.json` (86 кодов, без дублей),
`web/vite.config.ts` (блок `input` M30 + entry `oauth` M50); `composition.py` и `openapi.yaml` слились автоматически, `uv.lock`
перегенерирован. Миграции в линейную цепочку `20261009_m17_memory → m30_connection_binding → m41_telegram → m50_oauth` (head).
Integrator:
- **граница секретов:** LiteLLM (dev-зависимость M50) при импорте вызывает `load_dotenv()` и в основном checkout подтягивал
  `.env` в процесс pytest (падали `tests/test_config.py`; в worktree сессий `.env` нет). Исправлено: `LITELLM_MODE=PRODUCTION` до
  импорта (`tests/conftest.py`, тесты `tests/oauth`);
- `tests/plugins/google-calendar` → `tests/plugins/google_calendar`: дефис ломал `mypy` по проекту;
- запросы сессий: `KNOWN_EVENTS` + события M50/M41; ADR-0013 §4 — оба OpenAI-совместимых endpoint без `Idempotency-Key`;
  контрактный пример `device-tools/registration.json` и описания схем — `telegram.handoff` вместо `telegram.share`;
- OI-M15-003 RESOLVED для web (M30), OI-WEB-005 RESOLVED (M50); индекс OPEN_ISSUES дополнен OI-M25/M30/M40/M41/M42/M50,
  неснятые хвосты, адресованные M30/M40/M50, перенацелены;
- gate-тест `tests/integration/test_mw4_gate.py`: bot link/webhook M41, AS/apps/OpenAI-compat/`/mcp` M50 в одном продакшн-приложении,
  анонимный вызов получает challenge `resource_metadata`; пакеты Linear и Google Calendar проходят проверки публикации.

Gate MW4:
- web на новом API: PASS (M30: чат на `/v1/runs` + `/v1/feed`, магазин, подключения моделей; Playwright 28 passed — 23 M30 + 5
  согласие M50; install → OAuth → вызов в чате → подтверждение → результат; add-by-URL);
- Linear и Telegram в магазине: PASS офлайн (M40 — публикация и вызов на записанных ответах; M41 — `builtin.telegram`, e2e «две Яны»
  → подтверждение → handoff; device-цепочка на SM-S938B до `telegram.handoff`); живые проверки — UC-M40-1, UC-M41-1/2;
- OpenAI SDK работает через Mia: PASS (M50: OpenAI Python SDK, LiteLLM in-process и по сокету, SDK Python/JS; лимит расходов;
  `memory.context` без `memory.read` не отдаёт факты);
- чат на web и Android с голосовым вводом и озвучкой, в стиле M33: PASS (web — фейковые `SpeechRecognition`/`speechSynthesis`,
  экраны 390/768/1440 без горизонтального скролла; Android — device gate M25 на SM-S938B: диктовка → ответ в ленте озвучен,
  «Озвучить», голосовой режим и обратно); ощущение UI — UC-M25-1, UC-M30-1.

Проверки integrator: ruff, `ruff format --check`, mypy (444 файла), `validate_contracts.py` (mvp/v1: 106 operations, 86 error
codes) PASS; pytest с PostgreSQL+Redis 1294 passed / 5 skipped / 0 failed; alembic upgrade → downgrade `20261009_m17_memory` →
upgrade, `alembic check` без drift; web `tsc`, `npm run build` (entries `main` + `oauth`), Prettier PASS; Android `lint test
assembleDebug` PASS (377 тестов, 0 failed, 2 skipped — прежние).

Сессии PARTIAL только по живым проверкам: M40 (smoke с аккаунтом Linear), M41 (черновик Telegram на разблокированном телефоне,
реальный бот), M42 optional (нет преднастроенного Google OAuth-клиента — OI-M42-001, сервер не размещён — OI-M42-002). Для M60
они не блокируют MW5; закрываются UC-M40-1/UC-M41-1/UC-M41-2 до M60.

User checks MW4 — UC-M25-1…3, UC-M30-1…3, UC-M40-1, UC-M41-1/2, UC-M42-1, UC-M50-1/2 в [USER_CHECKS.md](USER_CHECKS.md); UC-MW2-4
теперь выполнима (действие с подтверждением — `builtin.telegram.send_message`).

Follow-ups MW4 (не блокируют MW5):
- Codex cross-review `app/modules/oauth/**` и `app/api/mcp_server.py` (рекомендация плана для M50) — не выполнен;
- в ленте нет `speech` (OI-M25-003/OI-M30-004) — аддитивное поле M18;
- M31 удаляет старый web POC (`lib/legacyApi.ts`, `workspace.tsx`, `Dashboard`, `plugins/**`, `app/web_poc`; список в handoff M30)
  и заменяет устаревший `design/scripts/capture_screenshots.mjs --seed` на `web/tests/screens.spec.ts`;
- карточка магазина `showcase.py` для `builtin.telegram` не добавлена;
- на телефоне скрипт gate M25 оставил ночной режим `auto` (прежнее значение неизвестно).

**Gate MW4 — PASS** (2026-10-09). Готовы к старту (волна MW5): M31, M24, M43, M51 (M51 начинает с вопроса пользователю о `git init`
MindWeaver).

MW5 стартовала 2026-10-09: M31, M24, M43, M51 параллельно (worktrees `../mia-helper-wt/{M24,M31,M43,M51-coordination}`, Android
`../mia-helper-android-wt/M24`, MindWeaver `../mind-map-generator-wt/M51`). Диспетчерские решения волны:
- MindWeaver: `git init` и baseline `3683744` по разрешению пользователя; в `.gitignore` добавлены `deploy/*.env*`,
  `.claude/settings.local.json`, IDE/кэши, `*.db-shm/-wal`; секретные файлы не открывались (проверка по именам и шаблонам ключей);
- M24 сам добавляет backend-endpoint обмена bearer → web-сессия (карточка: «M10-R или M30 по запросу», M30 его не сделал) —
  `app/api/v1/web_handoff.py`, строка регистрации в `composition.py`, аддитивно в контракты;
- отдельные PostgreSQL/Redis у каждой сессии (M24 55424/56324, M31 55431/56331, M43 55443, M51 55451/56351); телефон — под
  lock-каталогом, `adb reverse` на 8124.

**MW5 integrated 2026-10-09** (integrator: Opus 5.5 high вместо Sonnet 5.5 high по правилу «более сильная модель»). Backend merge:
M31 → `1c10c6c`, M24 → `39b317e`, M43 → `b623a41`, coordination M51 → `5d36258`, integration commit поверх. Android main: M24 →
`c23e2e5`, без конфликтов. MindWeaver main: M51 → `7d84b6f`, без конфликтов. Конфликт только ожидаемый: импорты `app/composition.py`
(`install_tiles` M31 + `install_web_handoff` M24); `openapi.yaml` и `tests/catalog/test_contract_routes.py` слились автоматически.
Новых миграций нет (head `20261009_m50_oauth`).
Integrator:
- стык M24↔M31: allow-list страниц hand-off содержал `privacy`, а страница M31 — `/data`; заменено на `data`, добавлен `more`
  (`app/modules/auth/web_handoff.py`, описание в `openapi.yaml`); пути `WebSurface.kt` совпадают с `App.tsx` → OI-M24-003 RESOLVED;
- стык M31↔M43: M31 отдаёт HTML плиток (bundled и `ui://` сервера) — OI-M43-001 RESOLVED, OI-M43-002 частично; таблица статусов
  и ограничения в `docs/PLUGIN_DEVELOPMENT.md` исправлены;
- OI-M14-006 RESOLVED (мост на настоящем хосте); OI-M15-009 DECIDED (решение M51, код Mia не менялся); индекс OPEN_ISSUES дополнен
  OI-M24/M31/M43/M51; скриншоты device gate M24 (`app/device-gate/results/*.png`) оставлены в Android-репозитории — только UI на
  тестовых данных;
- gate-тест `tests/integration/test_mw5_gate.py`: tile host и hand-off в продакшн-приложении; каждая страница hand-off — маршрут
  web; каждая web-поверхность Android — разрешённая страница (`MIA_ANDROID_DIR`); исходников `web_poc` нет; шаблон M43 проходит
  проверки публикации.

Gate MW5:
- дашборд MCP Apps: PASS (M31: хост MCP Apps — sandboxed `srcdoc` iframe с opaque origin, JSON-RPC мост, вызовы tools через router;
  Playwright: плитка таймера обновляется без LLM; iframe не читает cookies/DOM/localStorage host; память — подтвердить, удалить,
  экспорт). Отдельный origin для iframe не делался — отступление от карточки, OI-M31-001;
- Android WebView и push: PASS (M24, device gate на SM-S938B: магазин в приложении без повторного входа, установка «Таймера»;
  job → `PushChannel` → FCM-запрос `data.type=inbox_item` → уведомление → тап открывает карточку, в т.ч. с холодного старта).
  Вход Google, значение FCM-токена и доставка самим Google подставлены; живой FCM — UC-M24-1;
- MindWeaver через Mia: PASS локально (M51 E2E на интегрированном коде: 4 passed + 1 strict xfail — «Мия, сделай карту» → job →
  `build_map` через hosted `/mcp` MindWeaver → карта моделью пользователя через `/v1/chat/completions` → карта, inbox; отзыв гранта →
  `/mcp` 401 + `tool_unavailable`). Установка MindWeaver в Mia через add-by-URL и публикация не завершаются без правок Mia R1–R3
  (OI-M51-001) — M51 PARTIAL;
- `web_poc` удалён: PASS (`app/web_poc` и 58 тестов удалены; соответствие 71 прежнего теста — в handoff M31).

Проверки integrator: ruff, `ruff format --check`, mypy (445 файлов), `validate_contracts.py` (mvp/v1: 110 operations, 88 error codes)
PASS; pytest с PostgreSQL+Redis 1299 passed / 5 skipped / 0 failed (в первом прогоне — только известный flaky
`tests/jobs/test_process.py`); gate MW5 + `tests/web_handoff` + contract routes 28 passed; alembic upgrade на чистой БД, `alembic check`
без drift; web `npm run build`, Prettier PASS, Playwright 54 passed; Android main `lint test assembleDebug` PASS (422 теста, 0 failed,
2 skipped — прежние); MindWeaver `unittest` 96 OK; E2E M51 4 passed + 1 xfail.

User checks MW5 — UC-M24-1…3, UC-M31-1…3, UC-M43-1, UC-M51-1/2 в [USER_CHECKS.md](USER_CHECKS.md).

Follow-ups MW5:
- **правки Mia R1–R3 для M51** (OI-M51-001, OI-M15-009, OI-M43-004): resource indicators плагинов в AS, `/oauth/userinfo` для таких
  токенов (`aud`/`azp`), источник `mia_token` (5-мин. токен из гранта, `remoteServer.clientId` в манифесте). Диффы — handoff M51.
  Это identity/безопасность → repair-сессия M51-R1 (Opus 5.5 high), затем установка MindWeaver по URL;
- OI-M31-003: `Dockerfile.web` (CMD `app.web_poc.main`), `deploy/*`, `design/scripts/capture_screenshots.mjs` ссылаются на удалённый
  POC — **пересборка опубликованного POC сломается**; текущий деплой работает. Владелец — M62;
- OI-M24-001 / OI-M30-002: OAuth-подключение плагина из Android не завершается;
- OI-M31-005: web-чат теряет сообщение, отправленное до загрузки ленты (баг M30);
- Codex cross-review M50 (с MW4) не выполнен.

**Gate MW5 — PASS** с одним хвостом (M51 PARTIAL: установка по URL ждёт R1–R3). M60 зависит от M24, M25, M31, M40, M41, M51; из них
M40, M41 (живые проверки UC-M40-1, UC-M41-1/2) и M51 (R1–R3) — PARTIAL, поэтому M60 остаётся PENDING. Следующий шаг — repair M51-R1
и живые проверки M40/M41.

**M51-R1 integrated 2026-10-09** (integrator: Opus 5.5 high). Merge M51-R1 → `823f33c`, MindWeaver → `857cc5b`, без конфликтов; новых
миграций нет. Сделано: AS принимает server URL плагина как RFC 8707 resource (только из вычитанных данных: релиз магазина — для его
`clientId` и клиента подключений Mia; add-by-URL — только для клиента подключений Mia; плюс `OAUTH_EXTRA_RESOURCES` для dev);
`/oauth/userinfo` отдаёт `aud`/`azp`, собственные `/v1/*` и `/mcp` Mia токен с чужим audience отклоняют; `server.auth: mia_token` —
токен ≤5 мин без refresh из гранта клиента плагина (`remoteServer.clientId`), отзыв гранта гасит выпущенные токены. MindWeaver:
`clientId` в `mia/mia.json`, `/mcp` отклоняет токен другого приложения без `aud`. Integrator применил web-дифф OI-M51-R1-001
(«Подключить» для `mia_token` без страницы согласия) + Playwright-тест.
Проверки: ruff, format, mypy (449 файлов), контракты (110 operations, 89 error codes) PASS; pytest 1325 passed / 4 skipped / 0 failed;
E2E Mia ↔ MindWeaver 7 passed (xfail снят); MindWeaver unittest OK; web `tsc`, Prettier, Playwright 55 passed.
OI-M51-001/002, OI-M15-009, OI-M43-004, OI-M51-R1-001 RESOLVED. **M51 → COMPLETED.** Новые ручные проверки UC-M51R1-1/2.
M60 теперь ждёт только живые проверки M40 и M41 (UC-M40-1, UC-M41-1/2), после них — `Выполни M60`.

**Живые проверки на локальном стенде 2026-10-10** (пользователь, SM-S938B; стенд — `~/PycharmProjects/mia-stand`: backend + worker +
web на Mac, телефон через `adb reverse`, вход Google и ChatGPT-план подключены, встроенные плагины опубликованы). Итоги — в
[USER_CHECKS.md](USER_CHECKS.md):
- UC-MW2-3 **FAIL**: голосовой режим слышит свою озвучку и перебивает себя → OI-M22-001 ESCALATED (AEC выключен, системный TTS вне
  communication-пути, после TTS нет holdoff);
- UC-M25-1 **FAIL**: при вводе текста поле уходит к верху экрана, ленты не видно (нет `adjustResize`, IME учитывается дважды:
  `enableEdgeToEdge` + `safeDrawing` в `MiaScreen`); после выхода из голосового режима боковая кнопка не ведёт в чат (задача
  ассистента стартует с голосового экрана без кнопки «В чат», повторное нажатие игнорируется — `MiaApp.kt:87,114,295`);
- UC-M25-2 **FAIL**: диктовка обрывается через 480 мс тишины, одна фраза на нажатие, хвост в буфере захвата теряется при стопе,
  pre-roll 80 мс режет начало первого слова (`Dictation.kt:95,148-152,213-231`, `PcmVoiceActivityDetector.kt:32-39`).

Repair (параллельно, write scopes не пересекаются):
- **M22-R1** — эхо и barge-in голосового режима (Opus 5.5 high, аудио). Scope Android: `core/audio/**` (владелец
  `PcmVoiceActivityDetector`, в т. ч. настоящий pre-roll для всех потребителей), `core/tts/**`, `feature/voice/**`. Подход: TTS через
  свой `AudioTrack` в communication-пути (`USAGE_VOICE_COMMUNICATION`/`MODE_IN_COMMUNICATION`) + захват `VOICE_COMMUNICATION` с AEC,
  либо holdoff после Started/Done и адаптивный порог по измеренному эху; device gate с реальным динамиком;
- **M25-R1** — чат Android (Sonnet 5.5 high → Opus 5.5 high по правилу эскалации repair). Scope: `feature/chat/**` (диктовка: свой
  `VadConfig` ~1,2–1,6 с тишины, несколько сегментов до стопа, дочитать буфер при `finish()`, mic до `stt.prepare()`),
  `app/**` (`windowSoftInputMode=adjustResize`, одна обработка insets, из голосового экрана задачи ассистента — переход в чат),
  `feature/assistant/**` (интенты запуска). VAD-класс не меняет — только передаёт конфиг.
- **M24-R1** — окно согласия установки плагина в WebView приложения высотой с поле ввода (UC-M24-2; Sonnet 5.5 high → Opus 5.5 high
  по правилу эскалации repair). Scope: Android `feature/web/**` и web-стили диалогов (`web/src/styles/app.css`, `StoreConsent.tsx`) —
  сначала воспроизвести в WebView и в мобильной эмуляции Playwright, понять, чья ошибка (WebView viewport/`dvh` или CSS диалога).
- Структура разделов в приложении (вкладка «Чат» внутри «Дашборда», что в настройки, что во вкладки) — OI-UX-001, решение
  пользователя позже, не критично.
Повторная проверка — UC-R-0110.

**Repairs + тестовый деплой integrated 2026-10-10** (integrator: Opus 5.5 high). Backend main: M22-R1, M25-R1, M24-R1
(coordination + web-правка диалогов), M62-T1 → `e4539f7`, без конфликтов. Android main: M22-R1, M25-R1, M24-R1 → `e4a728d`, без
конфликтов. Проверки: ruff, format, mypy (453 файла), контракты (111 operations, 91 error codes) PASS; pytest 1347 passed /
4 skipped / 0 failed; web `tsc`, build, Prettier PASS, Playwright 64 passed / 2 skipped; Android `lint test assembleDebug` PASS
(474 unit-теста, 0 failed, 2 skipped).
- **M22-R1:** голосовой режим в communication-пути (`MODE_IN_COMMUNICATION`, `VOICE_COMMUNICATION` + AEC), своя озвучка через
  `AudioTrack`, holdoff 400/500 мс, адаптивный порог; на SM-S938B 0 ложных barge-in на 6 ответах (было 100 на 2), STT через динамик
  WER 2,3 % (было 14 %); настоящий pre-roll VAD. OI-M22-001 RESOLVED; double talk живым голосом — OI-M22-R1-001 (UC-R-0110).
- **M25-R1:** `adjustResize` + одна обработка insets (лента видна над клавиатурой); диктовка: пауза 1,4 с, автоконец 3,5 с, 400 мс
  pre-roll, дочитывание хвоста, микрофон за 21 мс; на голосовом экране всегда «Чат» (из задачи ассистента — обычная задача
  приложения, на заблокированном — PIN).
- **M24-R1:** WebView был WRAP_CONTENT → `100dvh = 0`, окно согласия 49 px; теперь MATCH_PARENT, диалоги `max-height: 90%`,
  согласие во весь экран телефона (695/727 px).
- **M62-T1:** MVP заменил POC на https://mindmapgen.xyz/mia/ (API+web, worker, Postgres, Redis ≈330 МБ; свободно 808 → 750 МБ из
  1967), cookie `Path=/mia/`, issuer `https://mindmapgen.xyz/mia` с well-known в корне, ключи в `/opt/mia/secrets` (600), POC SQLite
  — резервная копия; ChatGPT по подписке из «Модели» (вставка адреса возврата; device code для этого клиента OpenAI нет);
  `/mia/downloads/mia.apk` + «Скачать для Android». Runbook — `docs/DEPLOY_TEST.md`, откат — `publish-via-shared-server.sh --rollback`.
- Выкладка integrator-а 2026-10-10: образ из `e4539f7`, APK `0.1.0 (e4a728d)` с адресом сервера — PASS: образ `sha256:205ea2fc…`, release-mia.sh (миграции, health, `nginx -t`) без отката; снаружи `/mia/health/ready` 200, `/mia/` 200, анонимный `/mia/v1/feed` 401, APK 41 996 279 байт; `/`, `/source-refiner/`, `/knowly/` — 200. Дальнейшие ручные проверки — на сервере (UC-R-0110, UC-M62T1-1…3).

**Решения пользователя 2026-10-10 после проверки на сервере** → волна **MW5b** (карточки — MVP_SESSION_PLAN.md):
ADR-0016 (облако по умолчанию; действия и данные устройства — только по запросу, согласие только на целевом устройстве,
разово или постоянно для конкретного клиента; WebView = то же устройство), ADR-0017 (отдельные чаты как в Gemini, вызов
кнопкой — новый диалог с «Продолжить прошлый», общие задачи/уведомления/память, хранение 60 дней), встроенный поиск,
короткие вопросы без фоновой задачи, журнал вызовов инструментов, полоса «Сейчас». Wake word — после MVP.
Разбор «сводки новостей»: модель увела вопрос в фоновую задачу, `builtin.news.refresh` вернул «за 48 ч не найдено»,
общего поиска не было (`builtin.internet` не установлен); сеть сервера в порядке; аргументы вызова не журналируются.
Telegram в WebView не работает: web-сессия WebView не знает устройство (ADR-0016 §3 это исправляет).
M60 теперь зависит и от MW5b.

**Живые проверки 2026-10-10 на сервере (бот и Linear):** Telegram-бот настроен (`@mia_helper_ai_bot`, webhook без ошибок), но
«подключи Telegram-бота» не показывает ссылку — `connect_bot` отдаёт её только в `structuredContent`, клиенты её не выводят
(repair M41-R1). Linear добавлен по URL и подключён (OAuth), но модель его не видит: у сервера 76 инструментов, манифест
допускает 64, установка молча пропускается при каждом run (repair M15-R1). Логи сервера заменяют имена событий на
`redacted_event` — исправляет M15-R1.

**M41-R1 + M15-R1 integrated 2026-10-10** (integrator: Opus 5.5 high). Backend main → `0624293`, Android → bf7666d, без
конфликтов. Проверки: ruff, format, mypy (458 файлов), контракты (112 operations, 91 error codes) PASS; pytest 1385 passed / 4 skipped,
1 failed — известный flaky `tests/jobs/test_process.py` под нагрузкой Gradle, отдельно 3/3 PASS; web `tsc`, build, Prettier PASS,
Playwright 68 passed / 2 skipped; Android `lint test assembleDebug` PASS (481 тест, 0 failed). Выкладка: образ `sha256:973e7af5…`, APK `0.1.0 (bf7666d)` 42 013 139 байт — PASS без отката; снаружи `/mia/` 200, health 200, анонимный `/mia/v1/feed` 401, соседние приложения 200; установка Linear по URL (76 tools) на сервере теперь разбирается, webhook бота без ошибок. Проверки пользователя — UC-M41-2, UC-M40-1.

**MW5b-1 частичная интеграция 2026-10-10** (integrator: Opus 5.5 high): M73 → `61ed80e`, M74 → `cd6706f`, без конфликтов; миграция `20261010_m74_chats` — head. Проверки: ruff, format, mypy, контракты (mvp/v1: 121 operations, 92 error codes) PASS; pytest с PostgreSQL+Redis 1436 tests, 0 failures, 6 skipped. M75, M76 запущены от этого main; M70 интегрируется отдельно.

**MW5b integrated 2026-10-10** (integrator: Opus 5.5 high; сессии — отдельными агентами по модели карточки: M70, M74 Opus 5.5
high; M73, M75, M76, M71, M71-F1, M77 Sonnet 5.5 high; M72 Sonnet 5.5 medium). Порядок merge: M73, M74 → M70 → M75, M76 → M71 → M72
→ M77 → M71-F1. Конфликты только аддитивные: `error-codes.json` (M74/M70), `plugin-manifest.schema.json` (M72 `whereItRuns` + M77
`provides`). Миграции в линейную цепочку `20261009_m50_oauth → 20261010_m74_chats → 20261010_m70_device_access →
20261010_m77_notify_channels` (head). Правки integrator-а по запросам сессий: `device_consent` в `approval.requested` (OI-M70-001),
«Пока вас не было» в явно выбранном чате (OI-M74-001), push будильника/таймера data-only с TTL 600 с (OI-M71-001). M77 добавлена в
волну планом от 05:31 (коммит `67c5bf4`); Android-экран «Уведомления» — follow-up M71-F1.

Gate MW5b — PASS офлайн, живые проверки за пользователем:
- ruff, format, mypy (280 файлов app), контракты (mvp/v1: 40 examples, 14 run events, 129 operations, 101 error codes) PASS;
  pytest с PostgreSQL+Redis на свободной машине **1532 passed, 6 skipped, 0 failed**. Под нагрузкой Gradle/Playwright дважды
  наблюдались сбои: `test_twenty_parallel_runs_do_not_mix_events[postgres]` (таймаут 10 с; отдельно 3/3 PASS за 2–4 с) и одно
  зависание полного прогона (сессии `idle in transaction` на `runs`), не воспроизвелось — кандидат в M60 (OI-MW5b-001 ниже);
- web: `tsc -b`, build, Prettier PASS; Playwright **95 passed, 2 skipped** (порты 5183/8110 — 5173 был занят чужим dev-сервером);
- Android main `05fff9d` (дерево = проверенная ветка M71-F1): `lint test assembleDebug` PASS, 691 unit-тест, 0 failed;
- согласие на устройстве, «Где работает», встроенный поиск, журнал инструментов, чаты, «Сейчас», каналы уведомлений — покрыты
  тестами; device gates M71/M76 не выполнены (телефон не подключён) → UC-M71-1, UC-M71-F1, UC-M76-1. Живой FCM на сервере не
  настроен (UC-M24-1), без него согласие и звонок будильника в фоне на сервере не проверить.
Выкладка integrator-а 2026-10-10 (по разрешению пользователя): образ из `9c867cb`, `sha256:b9bad618…`, APK `0.1.0 (05fff9d)`
42 362 406 байт — PASS без отката. Миграции `m74_chats → m70_device_access → m77_notify_channels` применены; снаружи `/mia/health/ready` 200,
`/mia/` 200, анонимные `/v1/feed`, `/v1/now`, `/v1/chats`, `/v1/notifications/settings` — 401; `/`, `/source-refiner/`, `/knowly/` — 200;
issuer `…/mia`. Webhook бота перерегистрирован (`message`, `callback_query`; OI-M77-001). Логи API/worker за 15 мин — 0 ошибок; память
API 131/384 МБ, worker 129/256 МБ, свободно 839 МБ из 1967. Живой FCM на сервере не настроен (UC-M24-1). Решения пользователя по
вопросам MW5b — рекомендации приняты: OI-M75-006, OI-M73-001, OI-M74-003 → DECIDED.
UC-M41-2 повтор после выкладки MW5b — **PASS** (пользователь 2026-10-10: таймер приходит в бота).
**Живая проверка MW5b 2026-10-11** (UC-MW5b-live в USER_CHECKS): бот с таймером PASS, остальное — 6 замечаний → repair M74-R1, M72-R1, M75-R1, M76-R1 (карточки в MVP_SESSION_PLAN). M60 ждёт их.
Проверки пользователя MW5b: UC-M73-1, UC-M74-1, UC-M75-1, UC-M76-1, UC-M71-1, UC-M71-F1, UC-M72-1, UC-M77-1, повтор UC-M41-2.

| ID | Wave | Status | Dependencies | Preferred executor | Handoff | Integrated commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| M00 | MW0 | COMPLETED | — | Opus 5.5 high | [M00](handoffs/M00.md) | `3f904b6` | ADR-0010…0015, contracts/mvp/v1, ports+fakes |
| M01 | MW0 | COMPLETED | — | Sonnet 5.5 high | [M01](handoffs/M01.md) | `f52cc2a` | основная = GPT-plan (SIWC), резерв GLM-5.3-Flash |
| M10 | MW1 | COMPLETED | M00 | Opus 5.5 high | [M10](handoffs/M10.md) | `839200e` | cookie+bearer → один user; workspace/installations/dashboard; перенос SQLite |
| M11 | MW1 | COMPLETED | M00,M01 | Sonnet 5.5 high | [M11](handoffs/M11.md) | `e12ce12` | gateway `chatgpt-plan`+GLM, envelope crypto; живой smoke gpt-plan PASS (пользователь) |
| M12 | MW1 | COMPLETED | M00 | Opus 5.5 high | [M12](handoffs/M12.md) | `e4f0cc6` | runs/events/SSE, профили, speech/display, `speech.ack`; реестр error-codes |
| M20 | MW1 | COMPLETED | M00 | Sonnet 5.5 high | [M20](handoffs/M20.md) | `6028f42` / Android `34e978b` | VIS + lock screen; device gate PASS на SM-S938B |
| M21 | MW1 | COMPLETED | M00 | Opus 5.5 high | [M21](handoffs/M21.md) | `8c037e6` / Android `522c517` | GigaAM-v3 CTC, sherpa-onnx 1.13.8, загрузка модели при первом запуске |
| M13 | MW2 | COMPLETED | M10,M12 | Opus 5.5 high | [M13](handoffs/M13.md) | `6dc045a` | router/policy/MCP/approvals; миграция `20261009_m13_tools` |
| M14 | MW2 | COMPLETED | M00,M10 | Sonnet 5.5 high | [M14](handoffs/M14.md) | `b6f2563` | 6 builtin + declarative host, конвертер v1/v2; пакеты в `app/plugins/builtin/` |
| M22 | MW2 | COMPLETED | M12,M20,M21 | Opus 5.5 high | [M22](handoffs/M22.md) | `216a7c7` / Android `1cc92e8` | каскад STT→run→TTS, approvals, barge-in; device gate PASS (фейк модели) |
| M15 | MW3 | COMPLETED | M13,M14 | Sonnet 5.5 high | [M15](handoffs/M15.md) | `5f3a245` | магазин, OAuth для MCP, grants; миграция `20261009_m15_store` |
| M16 | MW3 | COMPLETED | M12,M13 | Opus 5.5 high | [M16](handoffs/M16.md) | `efe55c3` | worker, расписания, inbox, FCM v1; миграция `20261009_m16_jobs` |
| M17 | MW3 | COMPLETED | M10,M11,M12,M13 | Opus 5.5 high | [M17](handoffs/M17.md) | `6d3a44c` | Memory Ledger, people.resolve, retention, crypto-shred; миграция `20261009_m17_memory` |
| M23 | MW3 | COMPLETED | M13,M22 | Sonnet 5.5 high | [M23](handoffs/M23.md) | `8cb886d` / Android `1450389`, gate `f4be59b` | 4 device tools + backend endpoints; device gate PASS на SM-S938B после интеграции (UC-MW3-2) |
| M18 | MW3 | COMPLETED | M12 | Sonnet 5.5 high | [M18](handoffs/M18.md) | `9079c21` | `GET /v1/feed` (bearer+cookie, keyset); fix deadlock `open_turn` |
| M33 | MW3 | COMPLETED | M00 | Sonnet 5.5 high | [M33](handoffs/M33.md) | `294b1d9` / Android `3565859` | токены W3C → web+Android, MiaTheme, style-страница; ревью UC-MW3-1 |
| M30 | MW4 | COMPLETED | M15,M18,M33 | Sonnet 5.5 high | [M30](handoffs/M30.md) | `e86bc7b` | web на `/v1`: чат+лента, голос, подтверждения, магазин, модели; OAuth-привязка к браузеру; миграция `m30_connection_binding` |
| M25 | MW4 | COMPLETED | M18,M22,M33 | Sonnet 5.5 high | [M25](handoffs/M25.md) | `8d27ab0` / Android `fbc3e1a` | чат на runs+лента, диктовка, озвучка, UI M33; device gate PASS на SM-S938B |
| M40 | MW4 | PARTIAL | M15 | Sonnet 5.5 medium | [M40](handoffs/M40.md) | `9a91960` | Linear remote MCP + плитка; живой smoke — UC-M40-1 (OI-M40-001) |
| M41 | MW4 | PARTIAL | M15,M16,M17,M23 | Sonnet 5.5 high | [M41](handoffs/M41.md) | `30ceac5` | `builtin.telegram` + бот; миграция `m41_telegram`; черновик/бот вживую — UC-M41-1/2 |
| M42 | MW4 | PARTIAL | M15 | Sonnet 5.5 medium | [M42](handoffs/M42.md) | `cd6762f` | optional; пакет и тесты готовы; smoke ждёт OI-M42-001/002 |
| M50 | MW4 | COMPLETED | M11,M12,M13,M17 | Opus 5.5 high | [M50](handoffs/M50.md) | `293fc8f` | OAuth 2.1 AS + CIMD, OpenAI-compat, `/mcp`, SDK; миграция `m50_oauth`; Codex review рекомендован |
| M24 | MW5 | COMPLETED | M16,M22,M30 | Sonnet 5.5 high | [M24](handoffs/M24.md) | `39b317e` / Android `c23e2e5` | WebView + hand-off endpoint, FCM; device gate PASS; живой FCM — UC-M24-1 |
| M31 | MW5 | COMPLETED | M16,M17,M30 | Sonnet 5.5 high | [M31](handoffs/M31.md) | `1c10c6c` | MCP Apps host, память, входящие, «Где данные»; `web_poc` удалён |
| M43 | MW5 | COMPLETED | M15,M50 | Haiku 5.5 high | [M43](handoffs/M43.md) | `b623a41` | руководство, шаблон `plugins/_template` (публикация CLI в тесте), SDK quickstart |
| M51 | MW5 | COMPLETED | M15,M50 | Sonnet 5.5 high | [M51](handoffs/M51.md) | `5d36258` / MM `7d84b6f` | DoD локально PASS; add-by-URL и публикация — после M51-R1 |
| M51-R1 | MW5 | COMPLETED | M51 | Opus 5.5 high | [M51-R1](handoffs/M51-R1.md) | `823f33c` / MM `857cc5b` | plugin resources (RFC 8707), userinfo `aud`/`azp`, `mia_token`; E2E 7/7; web-дифф OI-M51-R1-001 применён integrator-ом |
| M22-R1 | MW5 | COMPLETED | M22, UC-MW2-3 | Opus 5.5 high | [M22-R1](handoffs/M22-R1.md) | `e4539f7` / Android `e4a728d` | communication-путь + AEC, своя озвучка, holdoff; 0 ложных barge-in; double talk — UC-R-0110 |
| M25-R1 | MW5 | COMPLETED | M25, UC-M25-1/2 | Opus 5.5 high | [M25-R1](handoffs/M25-R1.md) | `e4539f7` / Android `e4a728d` | adjustResize, диктовка с паузами, «Чат» из голосового экрана |
| M24-R1 | MW5 | COMPLETED | M24, UC-M24-2 | Opus 5.5 high | [M24-R1](handoffs/M24-R1.md) | `e4539f7` / Android `e4a728d` | WebView MATCH_PARENT (dvh=0), окно согласия во весь экран |
| M62-T1 | MW5 | COMPLETED | M51-R1 (main) | Opus 5.5 high | [M62-T1](handoffs/M62-T1.md) | `e4539f7` | MVP на mindmapgen.xyz/mia, APK для скачивания, ChatGPT из «Модели» |
| M41-R1 | MW5b | COMPLETED | M41 | Opus 5.5 high | [M41-R1](handoffs/M41-R1.md) | `0624293` / Android bf7666d | кнопки «только для экрана» (`_meta["mia/action"]`), «Открыть Telegram» в web/Android; ссылка не видна модели |
| M15-R1 | MW5b | COMPLETED | M15 | Opus 5.5 high | [M15-R1](handoffs/M15-R1.md) | `0624293` | лимит 96 на плагин и общий бюджет, выбор инструментов по URL, `needs_attention`, `/installations/{id}/health`, читаемые логи |
| M70 | MW5b | COMPLETED | ADR-0016 | Opus 5.5 high | [M70](handoffs/M70.md) | `4f98651` | политика устройств, согласие на устройстве, waiting_for_device, push с действиями |
| M73 | MW5b | COMPLETED | — | Sonnet 5.5 high | [M73](handoffs/M73.md) | `61ed80e` | встроенный поиск, короткие ответы без задачи, журнал инструментов |
| M74 | MW5b | COMPLETED | ADR-0017 | Opus 5.5 high | [M74](handoffs/M74.md) | `cd6706f` | чаты (API, лента/summary на чат, retention 60 дн.), `GET /v1/now` |
| M71 | MW5b | COMPLETED | M70 | Sonnet 5.5 high | [M71](handoffs/M71.md), [M71-F1](handoffs/M71-F1.md) | `d163c10`, `ebbc29e` / Android `0712b39`, `05fff9d` | Android: согласие, настройки разрешений, будильник Mia, WebView только `/mia/` |
| M72 | MW5b | COMPLETED | M70 | Sonnet 5.5 medium | [M72](handoffs/M72.md) | `cdc44e8` | магазин: «Где работает», возможности устройства |
| M75 | MW5b | COMPLETED | M74, M73 | Sonnet 5.5 high | [M75](handoffs/M75.md) | `140c3d3` | web: чаты, «Сейчас» |
| M76 | MW5b | COMPLETED | M74 | Sonnet 5.5 high | [M76](handoffs/M76.md) | `c714300` / Android `f24a87a` | Android: чаты, кнопка = новый диалог, «Сейчас» |
| M77 | MW5b | COMPLETED | M70 | Sonnet 5.5 high | [M77](handoffs/M77.md) | `f11f35a` | каналы уведомлений настраивает пользователь (каналы × типы событий), вопрос при подключении канала |
| M74-R1 | MW5b | IN_PROGRESS | M74, UC-MW5b-live | Opus 5.5 high | — | — | задача закрывается своим результатом; полный отчёт во «Входящих»; `FeedItem.steps` |
| M72-R1 | MW5b | IN_PROGRESS | M72, UC-MW5b-live | Sonnet 5.5 high | — | — | риск из аннотаций для установок до M72 (Linear) |
| M75-R1 | MW5b | IN_PROGRESS | M75, контракт `FeedItem.steps` | Sonnet 5.5 high | — | — | web: свёрнутые шаги/подтверждения, история шагов, озвучка целиком, отчёт во «Входящих» |
| M76-R1 | MW5b | IN_PROGRESS | M76, контракт `FeedItem.steps` | Sonnet 5.5 high | — | — | Android: переключение чатов, свёрнутые шаги, история шагов, озвучка целиком |
| M60 | MW6 | PENDING | M24,M25,M31,M40,M41,M51, MW5b | Opus 5.5 high | — | — | сквозная приёмка MVP; ждёт MW5b и живые UC-M40-1, UC-M41-1/2 |
| M61 | MW7 | PENDING | M60 | Sonnet 5.5 high | — | — | вторая модель |
| M62 | MW7 | PENDING | M60 | Sonnet 5.5 high | — | — | пилотный деплой |

## New web platform direction — 2026-10-05

По прямому запросу пользователя реализованы WEB-P1 + WEB-P1-R1 и код
WEB-P1-R2: React, семь плагинов, выбранная модель → tools → runtime, дашборд,
Интернет/новости, manifest v2 и SDK. Теперь есть страницы плагинов с реальными
скриншотами и отзывами, Google-only hosted auth, постоянные изолированные аккаунты,
установки/состояния/раскладка в SQLite, браузерная диктовка и озвучка.
Рабочее дерево на базе `c5fc60f`, отдельного integrated commit пока нет.

**WEB-P1-R2: COMPLETED для shared-server POC, 2026-10-06.** Опубликовано
на [https://mindmapgen.xyz/mia/](https://mindmapgen.xyz/mia/); плитка Мии видна в
общем хабе. Подключение выполнено после разрешения пользователя на операционное
использование env values только как переменных, без просмотра/вывода. Штатный
VPN/SSH tool получил пароль через окружение. Публичные проверки PASS: auth
required, anonymous workspace 401, реальные assets/screenshots 200, все пять
карточек хаба сохранены, соседние приложения 200/uptimes unchanged. Live Chromium:
плитка → Google login → OAuth redirect; no JS/resource errors, mobile no overflow.
Container healthy, UID 10001/read-only/384MB; SQLite directory 700; nginx valid.
Google consent в аккаунте владельца не выполнялся: это интерактивный шаг входа.

**WEB-P1-R3: COMPLETED, 2026-10-06.** Исправлен и опубликован 403 после Google
return: middleware разрешает только top-level GET/HEAD static entry; API/CSRF
checks сохраняются. Backend regression FAIL before / PASS after; 71 tests PASS.
Реальный Chromium 303 return regression FAIL на старом релизе / PASS на новом:
страница 200, UI загружается, anonymous workspace 401, cross-site API 403.
Публичный smoke PASS; MIA healthy, соседние apps/cards/uptimes сохранены.
Google config/callback не менялись. [R3](handoffs/WEB-P1-R3.md).

Текущая проверка: production build/TypeScript/Prettier PASS; 20 Chromium journeys
PASS; Ruff/format/mypy PASS; 58 web tests + 13 Google verifier tests = 71 PASS.
Реальный amd64 release container локально и на сервере healthy: обязательный Google-вход, anonymous
workspace 401, login/assets/screenshots через `/mia/`, mobile 390px без overflow.
Shell syntax/Compose/diff PASS. Платных обращений к моделям и реального Google
consent не было. Предыдущие RSS/schema/backend проверки остаются историческими.

План: [WEB_PLATFORM_PLAN](../WEB_PLATFORM_PLAN.md). Архитектура/SDK:
[PLUGIN_DEVELOPMENT](../PLUGIN_DEVELOPMENT.md). Запуск:
[web/README](../../web/README.md). Деплой: [deploy/README](../../deploy/README.md).
Свидетельства: [WEB-P1](handoffs/WEB-P1.md), [WEB-P1-R1](handoffs/WEB-P1-R1.md),
[WEB-P1-R2](handoffs/WEB-P1-R2.md), [WEB-P1-R3](handoffs/WEB-P1-R3.md). Ключи/подключения всё ещё в памяти до 8h/restart;
SDK grants до часа. Hosted loopback inference отключён до companion connector.
Новые вопросы в WEB-P1 inbox `OPEN_ISSUES.md`. Старые voice gates и готовность
S31-R1/S33 не изменены.

## Original Android/backend sessions

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
| S28 | W6 | SUPERSEDED | S27 | Sol xhigh | — | — | DEFERRED out of PoC scope (ADR-009); not started; SUPERSEDED by M20 (MVP plan) |
| S30 | W6 | COMPLETED | S12,S14,S21,S23 | Sol high | [S30](handoffs/S30.md) | d8db5ce | real sign-in → Gemini text → restart → retry PASS on SM-S938B |
| S31 | W6 | PARTIAL | S16,S25,S26 | Fable 5.1 xhigh | [S31](handoffs/S31.md) | d8db5ce / Android ed8e05f | voice made reachable; one real round trip; soak/barge-in/latency NOT measured |
| S32 | W6 | PARTIAL | S17,S23,S25 | Sol high | [S32](handoffs/S32.md) | 5b05d0f | text modality closed on device against real Gemini; voice modality waits on S31 |
| S31-R1 | W6 | SUPERSEDED | S31,S16-R2,S25-R3 | Fable 5.1 xhigh | — | — | acoustic closeout: soak, barge-in, latency, voice tool smoke; SUPERSEDED by M22/M60 (MVP plan) |
| S33 | W7 | SUPERSEDED | S18,S30,S31,S32 | Fable 5.1 xhigh | — | — | release gate; blocked on S31-R1; SUPERSEDED by M60 (MVP plan) |
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

> **Superseded 2026-10-09.** The MVP M-plan replaces this path: S31-R1 and S33
> are SUPERSEDED by M22/M60 (see the MVP section at the top). The next command is
> `Выполни все готовые сессии wave MW0 параллельно`. The text below is kept as
> history of the legacy voice beta gate.

**Legacy next command was `Выполни S31-R1`.** It has no session card in the plan —
it is a repair, so its input is [S31](handoffs/S31.md) "Incomplete work" plus
[S16-R2](handoffs/S16-R2.md) and [S25-R3](handoffs/S25-R3.md), which changed the
protocol and the playback path it must measure. It needs the phone at the sound
source or a person speaking to it, and nothing else.

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
