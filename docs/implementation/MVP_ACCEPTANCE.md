# Приёмка MVP (M60)

Дата: 2026-10-11. Исполнитель: Claude Opus 5.5 (high). Ветка `session/M60-mvp-acceptance`, база main `1860546`.
Handoff: [handoffs/M60.md](handoffs/M60.md). Вопросы — `## M60 inbox` в [OPEN_ISSUES.md](OPEN_ISSUES.md).

## Итог

**Статус MVP: `NOT READY`.**

Автоматически на продакшн-сборке приложения проходят сценарии 1, 4, 5, 6, 8 и web-часть 10. Не проходят или проходят
частично: 2 (голосом читается только два источника из трёх — нужно решение), 3 (в подтверждении вместо имени получателя
`person:<id>` — repair M41-R2), 7 (нет удаления аккаунта и переключателя инкогнито — repair M17-R1). Сценарий 9 и
телефонная половина 10 проверяются только на устройстве; живых замеров с основной моделью нет.

Что осталось до `READY`:

1. Repair-сессии (автоматические провалы): **M41-R2**, **M17-R1**; **M12-R1** — только если пользователь выберет вариант
   (a) или (b) в OI-M60-001.
2. Решение пользователя: OI-M60-001 (бюджет голосового хода для «Что у меня сегодня»).
3. Живые проверки (только пользователь): UC-M60-1…12 (предложены в handoff M60), а также уже открытые UC-M24-1 (живой FCM
   на сервере не настроен), UC-M40-1 / UC-M72-1 (живой Linear, OI-M72-R1-004), UC-M41-1 (черновик Telegram),
   UC-M51-1/2 и UC-M51R1-1/2 (нужна развёрнутая MindWeaver, OI-M51-004), UC-MW5b-R1.

## Как проверялось

Новый набор `tests/e2e_mvp/` собирает **то же приложение, что и сервер** — `create_app(settings)` с
`install_runtime_api`: auth, модели, магазин, router, approvals, память, runs, jobs и worker, чаты, Telegram, OAuth AS,
device tools — на настоящих PostgreSQL и Redis. Подменено только то, что вне Mia:

- Google ID-токены (`google:<sub>`), основная модель — сценарий по шагам (живой модели офлайн нет);
- удалённые MCP-серверы и их OAuth-провайдер — `tests/catalog/remote_world.py` с записанным набором инструментов
  Linear; ходят через настоящие `McpConnector` и `OAuthClient` (подменены только DNS-резолвер и транспорт);
- FCM (`FakeFcm`), Telegram Bot API (`FakeBotApi`);
- телефон — HTTP-вызовы Android-клиента (bearer-сессия, регистрация device tools из фикстуры M23, ответы на вызовы,
  решения подтверждений); события run читаются из того же потока, что отдаёт SSE (`httpx.ASGITransport` буферизует SSE).

Worker — `app.worker.main.Worker` поверх `app.state.jobs`; «рестарт» — второе приложение на той же базе.
Web — Playwright `web/tests/mvp-acceptance.spec.ts` на `web/tests/support/e2e_server.py`.

## Результаты по сценариям

| # | Сценарий | Автоматически (тесты) | Результат | Живая проверка | Ограничения / находки |
|---|---|---|---|---|---|
| 1 | Магазин: опубликовать → установить → подключить → плитка → голос | `test_s01_store_to_voice.py::test_publish_install_connect_tile_and_voice`: настоящий пакет `plugins/linear` публикуется CLI `cli.mia_plugin publish`; установка с согласием (digest, гранты) → `needs_connection`; OAuth (PKCE, resource) → `active`; плитка `my-issues` с CSP и мостом, вызов `list_issues` из плитки без модели; тот же дашборд в браузере (один аккаунт); голосовой run вызывает `team.linear.list_issues` (в голосе только `voice: true`), события те же по SSE | **PASS** | UC-M60-12; UC-M40-1, UC-M72-1 | Живой Linear: что он отдаёт в аннотациях — OI-M72-R1-004 |
| 2 | «Что у меня сегодня» (Linear, заметки, таймеры) | `test_s02_today.py::test_today_in_chat_reads_linear_notes_and_timer` (чат: три инструмента параллельно в одном шаге, ответ карточкой, «Сейчас» показывает таймер на web и телефоне, M73: `background.start` предложен, ответ прямой); `::test_today_by_voice_reads_two_sources_within_the_voice_budget`; strict xfail `::test_today_by_voice_reads_all_three_sources` | **PARTIAL** | UC-M60-1; UC-M73-1 | Голосом третий вызов получает `tool_budget_exceeded`: профиль `voice` — ≤ 2 tool calls, ≤ 3 шага (ADR-0011, `RUN_VOICE_MAX_TOOL_CALLS=2`). Это решение, не ошибка кода → OI-M60-001. Calendar (M42 optional, PARTIAL) не проверялся |
| 3 | «Напиши Яне…» с уточнением и подтверждением | `test_s03_yana.py::test_two_yanas_clarify_then_approve_on_the_phone_and_hand_off` (две «Яны» в памяти → `people.resolve` → вопрос без подтверждения и без вызова телефона → «Петровой» в том же разговоре → `approval.requested` с точными аргументами → неверный `args_hash` отклонён → «Одобрить» с телефона → `device_tool.call telegram.handoff {text, contact_name}` → ответ телефона → готово; номер телефона не попадает ни в события, ни в запросы модели; шаг в ленте с `channel: android`); `::test_denied_approval_sends_nothing`; `::test_another_account_cannot_decide_the_approval`; strict xfail `::test_confirmation_names_the_recipient_chosen_by_ref` | **FAIL** | UC-M60-2; UC-M41-1, UC-MW3-3, UC-MW2-4 | После уточнения модель передаёт `to="person:<uuid>"` (так велит `people.resolve`), и текст подтверждения показывает `to="person:3e6c…"` — пользователь не видит, кому уходит сообщение (`approval_summary`, `app/modules/tools/policy.py:96`, вызов `router.py:688`) → **M41-R2** |
| 4 | «Напоминай о просроченных задачах по будням в 9» — рестарт worker, доставка | `test_s04_schedule.py::test_weekday_schedule_survives_a_worker_restart_and_is_delivered_once` (голос → `schedule.create` с `RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9`, `next_run_at` — будний день 09:00 по часовому поясу; worker A остановлен, второе приложение и worker B: в 09:00 ровно одна задача, run задачи читает Linear, одна запись во «Входящих», один push FCM, одно сообщение бота со списком задач; следующий запуск — следующий будний день); `::test_a_job_held_by_a_dead_worker_is_finished_once_by_the_next` (lease истёк → B забирает, попытка 2, доставки по одной); `::test_occurrences_missed_while_no_worker_ran_are_skipped` (`skip`) | **PASS** (после safe fix) | UC-M60-3; UC-M24-1, UC-M41-2 | Safe fix: бот получал только заголовок и короткую фразу, без списка задач (`TelegramChannel.render` брал `speech`) → теперь полный текст результата (`app/modules/notifications/telegram.py:498-501`). Живой FCM на сервере не настроен (UC-M24-1). Какой RRULE выберет живая модель — живая проверка |
| 5 | Фоновая задача голосом и «пока вас не было» | `test_s05_background.py::test_voice_job_delivers_one_result_to_its_chat_and_is_announced_once` (голос → `background.start`, `job.created`, короткий ответ; задача в worker с `notify` внутри → один результат: одна запись «Входящих» с полным текстом, один push, одна карточка в исходном чате с `inbox_item_id`, ничего в другом чате; следующий интерактивный run получает «Пока пользователя не было…», следующий за ним — нет) | **PASS** | UC-M60-4; UC-M73-1, UC-MW5b-R1, UC-M24-1 | Выбор `background.start` живой моделью — живая проверка |
| 6 | MindWeaver голосом и из web | `test_s06_mindweaver.py::test_mia_token_plugin_by_voice_through_a_job_and_revocation` (плагин `server.auth: mia_token` с `remoteServer.clientId`: публикация, установка, «Подключить» без страницы согласия после входа через Mia, голос → `background.start` → worker → вызов инструмента плагина с токеном Mia для его клиента и ресурса → результат во «Входящих»; отзыв гранта → токен мёртв, инструменты исчезают из следующего run, вызовов больше нет). Сквозной E2E MindWeaver `backend/e2e_mia/test_mia_chain.py` на текущем коде Mia: **7 passed** (на копии теста с одной правкой, см. ограничения). Web «Подключить» для `mia_token` — `web/tests/journey.spec.ts` (M51-R1) | **PASS** (офлайн) | UC-M60-5; UC-M51-1/2, UC-M51R1-1/2 | Нужна развёрнутая MindWeaver (OI-M51-004). Тест MindWeaver отстал от Mia (`InstallService(registry=…)` с M15-R1) и не герметичен: из основного checkout подтягивает `backend/.env` MindWeaver → OI-M60-005. После отзыва гранта список подключений до проверки здоровья показывает «подключено» → OI-M60-006 |
| 7 | Память: просмотр, удаление, инкогнито, удаление аккаунта | `test_s07_memory.py::test_remember_confirm_view_everywhere_delete_and_forget` («Запомни, что Яна — моя жена» → предложено → подтверждение в браузере → видно на телефоне и в web, в экспорте и в контексте следующего run; удаление факта убирает его отовсюду; «Забыть всё»); `::test_incognito_run_remembers_nothing_and_leaves_no_history` (API: `memory_incognito`, нет `conversation_id`, текст не хранится, ленты нет); `::test_account_deletion_crypto_shreds_memory_at_the_service_level`; strict xfail `::test_the_user_can_delete_the_account`, `::test_the_web_chat_can_start_an_incognito_run` | **FAIL** | UC-M60-6, UC-M60-7 (после M17-R1) | Удаления аккаунта нет ни в API, ни в web, ни в Android (только `MemoryService.shred_account`, OI-M17-004, OI-M15-006). Инкогнито есть только в API и кодеке Android, переключателя нет нигде → **M17-R1** |
| 8 | Негативные проверки безопасности (Review §5.3, Architecture §24) | `test_s08_security.py` — 8 тестов: изоляция арендаторов по HTTP (телефон и браузер чужого аккаунта: 403/404/409 на ~30 операций, свои списки без чужих данных); плитка вызывает только свои read-инструменты, токены подключений не уходят чужим, токены Mia/cookie/токен бота не уходят плагину; add-by-URL на metadata/частные адреса/DNS-rebinding — 0 запросов наружу; `write_external` ждёт одно точное подтверждение в профилях chat, voice и job, подменённый `args_hash` → 409, повторное противоположное решение → 409, ровно один внешний вызов, аудит с `operation_id` без содержимого; истёкшее подтверждение ничего не делает; таймаут записи → `uncertain` без повтора; prompt injection в результате инструмента остаётся данными (`trust="untrusted"`, поддельный закрывающий тег обезврежен), «послушная» модель упирается в подтверждение; секреты (токены подключений, bearer, токен бота, webhook secret) не появляются в событиях, SSE, ленте, запросах модели и логах INFO; анонимные `/v1/*`, `/mcp` → 401 с challenge; CSRF cookie с чужого Origin → 403; токен приложения без `memory.read` памяти не получает, audience соблюдается | **PASS** | — | Гигиена (не дефекты): add-by-URL сохраняет строку с буквальным частным IP и отказывает только при discovery (OI-M60-007); тег `Apps` у `/v1/memory/context` в контракте (OI-M60-008) |
| 9 | Голосовые замеры: p50/p95, 20 ходов, barge-in, батарея за час | — (только устройство; телефон не подключён) | **NOT VERIFIED** | UC-M60-8, UC-M60-9, UC-M60-10 | Прежние замеры только с моделью-заглушкой 0 мс (M22: конец речи → первый звук p50 804 / p95 836 мс, barge-in ~89 мс; M22-R1: 0 ложных barge-in на 6 ответах; STT p50 106 мс). С живой основной моделью и батарея — никогда. Цель Review §3.2: p50 ≤ 2,5 с; ~6 с до первого звука (ADR-0011); p95 и батарея — предложения в UC. Экран голоса показывает «Первый звук через N мс» (`CascadeVoiceRoute.kt:207`) |
| 10 | Чат с голосовым вводом и озвучкой на web и телефоне; один стиль 390 / 768 / 1440 | `web/tests/mvp-acceptance.spec.ts` — 3 теста: при 390×844, 768×1024, 1440×900 в светлой и тёмной теме — диктовка (фейковый `SpeechRecognition`) → отправка → ответ из нескольких абзацев → автоозвучка читает весь ответ (M75-R1), «Остановить»/«Озвучить»; без горизонтальной прокрутки на чате, магазине, дашборде, памяти, «Входящих», «Ещё»; шрифт, акцент, фон, радиусы и цвет кнопки одинаковы на трёх ширинах; темы различаются фоном и текстом. Android: JVM-тесты `feature/chat` (`ChatPresenterTest`, `MicDictationTest`, `TtsChatSpeakerTest`, `ChatScreenTest`) и device gate M25/M25-R1 `ChatDeviceGateTest` | web **PASS**, телефон — живая проверка | UC-M60-11; UC-M25-1/2 (повтор UC-R-0110), UC-M30-2 | Косметика: прямоугольная рамка фокуса внутри скруглённого поля ввода (на ревью дизайна, OI-M60-011) |

## Исправления в рамках safe fix

| Дефект | Файл | Регрессионный тест |
|---|---|---|
| Бот Telegram получал только заголовок и `speech` результата задачи (список задач терялся) | `app/modules/notifications/telegram.py:51,498-501` | `tests/e2e_mvp/test_s04_schedule.py::test_weekday_schedule_survives_a_worker_restart_and_is_delivered_once` |
| Гонка: при медленной записи «Секунду.» (`speech.ack`) оказывалась после `approval.requested` и звучала поверх подтверждения (OI-M22-002); найдено нагрузочным прогоном OI-MW5b-001 | `app/modules/runs/engine.py:557-560` (флаг под `final_lock`) | `tests/e2e_mvp/test_oi_mw5b_001.py::test_no_filler_after_the_approval_prompt_when_the_ack_write_is_slow` (детерминированно воспроизводит без нагрузки) |
| Тесты, падающие под нагрузкой: `test_no_speech_ack_while_waiting_for_approval` требовал полного отсутствия ack (правило — не после подтверждения); `test_now_lists_active_timers_alarms_and_jobs` ставил таймер на +60 с от момента сбора тестов | `tests/runs/test_engine.py:277-280`, `tests/chats/test_now.py:83-84,135` | сами тесты (3/3 под нагрузкой 12 процессов) |

## OI-MW5b-001: диагностика

- Детектор вложенных checkout пула (pytest-плагин вне репозитория: задача берёт второе соединение, пока держит первое)
  на `tests/runs tests/chats tests/feed tests/jobs tests/notifications tests/device_access tests/memory tests/tools` и на
  составном приложении (`tests/e2e_mvp`, `tests/catalog`, `tests/oauth`, `tests/plugins`, `tests/integration`,
  `tests/tiles`, `tests/workspace`): **0 вложенных checkout в коде Mia** (около 11 000 checkout). Гипотеза «сессия держит
  транзакцию и ждёт вторую из того же пула» не подтвердилась.
- Нагрузка 10–12 процессов на 10 ядер, `tests/runs tests/chats tests/feed tests/jobs tests/notifications` 6 раз подряд,
  наблюдение `pg_stat_activity` каждые 5 с: ни одной сессии в `idle in transaction` дольше 15 с, зависаний нет;
  `test_twenty_parallel_runs_do_not_mix_events[postgres]` 5/5 по ~1,05 с под нагрузкой. Падали только два
  чувствительных ко времени теста (выше) и, до исправления, — настоящая гонка `speech.ack`.
- Сознательная утечка задач run в конце теста (20 runs без `aclose`) зависания следующего теста не вызывает:
  pytest-asyncio отменяет задачи, транзакции откатываются.
- Наблюдение: внутри транзакции есть сетевые вызовы — обновление OAuth-токена подключения
  (`app/modules/connections/oauth/service.py:681`) и токена ChatGPT-плана (`app/modules/models/chatgpt_plan/credentials.py:158,191`):
  пока провайдер отвечает, соединение пула «idle in transaction» (ограничено таймаутами 10 с). На `runs` не влияет.
- Вывод: зависание MW5b не воспроизведено; рекомендуется (c) из OI — `pytest-timeout` в dev-зависимостях и
  `lock_timeout` для `TRUNCATE` в фикстурах, чтобы будущее зависание падало с трассой (общие файлы, OI-M60-004).

## Repair-сессии

### M41-R2 — Подтверждение называет получателя

- **Executor:** Opus 5.5 high (путь подтверждений — безопасность).
- **Objective:** текст подтверждения и голосовая фраза показывают имя человека (`Яна Петрова`), а не ссылку
  `person:<id>` / `contact:<n>`; аргументы и `args_hash` не меняются.
- **Write scope:** `app/modules/tools/{policy,router}.py` (резолвер имён для summary, привязка поздняя),
  `app/modules/memory/**` (имя по ссылке, только владельцу), одна строка в `app/composition.py`, `tests/tools/**`.
- **DoD:** strict xfail `tests/e2e_mvp/test_s03_yana.py::test_confirmation_names_the_recipient_chosen_by_ref` проходит
  (в summary «Яна Петрова», нет `person:`); тесты router и Telegram зелёные; web и Android показывают `summary` без правок.

### M17-R1 — Удаление аккаунта и инкогнито для пользователя

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

### M12-R1 — Бюджет голосового хода (условно, после решения OI-M60-001)

- **Executor:** Sonnet 5.5 high.
- **Objective:** голосовой «Что у меня сегодня» читает 3–4 источника за один ход, не выходя из ~6 с до первого звука.
- **Write scope:** `app/core/config.py` (значение по умолчанию) и/или `app/modules/runs/engine.py` (параллельные
  read-вызовы одного шага считаются одним), `docs/adr/0011-*.md` (поправка), `tests/runs/**`.
- **DoD:** strict xfail `tests/e2e_mvp/test_s02_today.py::test_today_by_voice_reads_all_three_sources` проходит;
  существующие тесты лимитов голоса обновлены; запись (`write_*`) по-прежнему считается отдельно.

### M22-R2 — Экспорт голосовых замеров (необязательно, не блокирует)

- **Executor:** Sonnet 5.5 high.
- **Objective:** экран голоса умеет скопировать/поделиться последними 20 `TurnMetrics` (первый звук, STT, старт run,
  TTS), чтобы UC-M60-8 не зависела от ручного переписывания.
- **Write scope:** Android `feature/voice/**`.
- **DoD:** unit-тест форматирования экспорта; кнопка в debug-сборке или по долгому нажатию.

## Проверки gate (M60)

- `ruff check .`, `ruff format --check .`, `mypy` (519 файлов), `validate_contracts.py` (130 operations, 102 error codes) — PASS;
- полный `pytest` с PostgreSQL+Redis: 1580 тестов — 1570 passed, 6 skipped, 4 xfailed (strict, находки M60), 0 failed;
- `tests/e2e_mvp`: 23 passed, 4 xfailed;
- web Playwright: 105 passed, 2 skipped; `tsc -b`, Prettier — PASS;
- MindWeaver E2E (копия с `registry=registry`, исходники `git archive`): 7 passed.

Подробно — handoff M60, раздел «Verification».

## Повтор после repair MW6 (2026-10-11, integrator)

M12-R1, M41-R2, M17-R1, M22-R2 влиты (`ef6e978`, Android `f96d681`). `tests/e2e_mvp`: все strict xfail сняты и проходят —
сценарий 2 (голосом три источника), 3 (подтверждение называет «Яна Петрова (жена)»), 7 (удаление аккаунта, инкогнито из web)
теперь PASS автоматически. Полный pytest 1598 passed, 6 skipped, 0 failed; Playwright 108 passed, 2 skipped; Android 734
теста, 0 failed. **Автоматическая часть MVP — READY.** Статус MVP целиком — по-прежнему NOT READY до живых проверок
(UC-M60-1…12, UC-M17R1-1, UC-M24-1) и волн MW6b (wake word, «Устройства», Personal Node) и MW7 (вторая модель, пилотный деплой)
— по решению пользователя они входят в MVP.
