# Personal Node: точки интеграции в коде

M85, снимок `main` `61353a8`. Только места, которые будут менять сессии M86–M91, и то, что в них мешает.

## Композиция и процессы

- `app/main.py:create_app` строит `Runtime(Database, RedisClient, migration_head())` и вызывает
  `install_runtime_api` (`app/composition.py:129`).
- `app/worker/main.py:serve()` — тот же `create_app` плюс 4 цикла `Worker`.
- Узел M91: профиль `MIA_ROLE=node` выполняет следующее:
  - `RedisClient` → in-process реализация 4 методов (`app/core/redis.py`);
  - `config.py:160-174` разрешает пустой `REDIS_URL`;
  - циклы `Worker` запускаются в lifespan;
  - `DATABASE_URL` указывает на встроенный Postgres.
- Run engine, канал устройств, ожидание подтверждений и liveness живут **в памяти процесса**. Примеры:
  `InMemoryDeviceToolChannel._pending`, `DeviceAccess._seen`, `RunNotifier`. В облаке управляющий канал узла должен
  жить в процессе API, который исполняет run. При одном процессе API (сейчас так) это просто. При нескольких нужна
  маршрутизация между процессами (OI-M85-004).

## Устройства и согласие (ADR-0016, M70)

- `app/core/ports/device_tool_channel.py`: `DeviceToolCall.delivery` = `stream | push | None`. M87 добавляет `node`.
- `app/modules/tools/device.py:InMemoryDeviceToolChannel.invoke` (l.170) решает, куда отправить вызов:
  - сейчас: поток run или push;
  - M87: `node` → `rpc tool.call` по управляющему каналу; результат возвращается напрямую, без
    `POST /v1/runs/{id}/device-tool-results`.
- `app/modules/tools/device_policy.py`:
  - `CAPABILITIES` (l.55) — жёсткий словарь; M87 добавляет `files.search`, `files.read`, `apps.open` на узле,
    `links.open`;
  - `decide()` (l.128) переиспользуется без изменений, где target = узел.
- `app/modules/tools/device_access.py`:
  - `target()` (l.207) выбирает последний телефон с объявленными инструментами — узлы нужно выбирать отдельно,
    по имени инструмента `node.*`;
  - `reachable()` для узла = открыт управляющий канал.
- `app/modules/tools/router.py`: `DEVICE_TOOL_NAME` (l.102) `^(device|telegram)\.` → добавить `node\.`;
  `_remote_device_call` (l.580) и `_request_device_consent` (l.646) подходят как есть.
- Согласие:
  - сейчас `device_consent.waiting_for` — константа `"device"` (`contracts/mvp/v1/device-access.schema.json`), а
    решать может только целевое устройство (`app/api/v1/approvals.py:149`, `device_gate`);
  - для узла: `waiting_for: "node"` (аддитивно) и правило «решает назначенный подтверждающий (телефон владельца)»
    (ADR-0019 §5).
- Таблица `devices` (`app/modules/auth/models.py:16`): `platform` по умолчанию `"android"`, а
  `DeviceInput.platform` = `Literal["android"]`. Узел — строка `devices` с `platform="node"` плюс таблица
  `nodes` (публичный ключ, host, `data_mode`, `approver`, `revoked_at`, `manifest`). Так узел сразу получает
  гранты, журнал доступа и настройки возможностей M70 (`/v1/devices/{id}/grants|access-log|capabilities`) и
  появляется в списке M84.

## Модели (M11)

- Порт `app/core/ports/model_gateway.py`: `stream(ModelRequest) -> AsyncIterator[ModelEvent]`.
- `app/modules/models/adapters/__init__.py:build_adapter`: `ollama` → `ChatOnlyAdapter` (`/api/chat`,
  `stream: false`, без tools). M88:
  - `ollama` → `ChatCompletionsAdapter` на `http://127.0.0.1:11434/v1` (для runtime на узле);
  - новый `NodeOllamaAdapter` (rpc `model.generate`) для облака.
- `app/modules/models/catalog.py:PROVIDERS` (l.50): `ollama` помечен `local=True` и `chat_only`;
  `resolve_base_url` (l.167) принимает только loopback.
  - В облаке loopback недопустим (SSRF, Architecture §6). Подключение `node-ollama` хранит `node_id`, а не URL.
- `app/modules/models/connections.py:resolve` (l.264): `mia:default` = основное подключение, затем остальные
  tools-capable по порядку. `node-ollama` исключается из автоматической цепочки резервов (ADR-0019 §6).
- `max_output_tokens` есть в `ModelRequest`, но run engine его не задаёт (`app/modules/runs/engine.py`) — M88.

## Run engine и SSE

- `app/modules/runs/domain.py:RunStatus` (l.16): `running, waiting_approval, …`. Ожидание устройства выражено
  подтверждением (`waiting_approval` + `device_consent.waiting_for`). Ожидание узла, который выключен:
  - интерактивный run не ждёт дольше 30 с переподключения и падает с `node_offline`;
  - задача (`job`) ставится в очередь с `run_after` (ADR-0019 §6);
  - новый статус run не нужен.
- `RunService.publish` (`service.py:131`) — точка, через которую узел и канал публикуют события.

## Хранение и задачи

- Сводка — [postgres-inventory.md](postgres-inventory.md).
- Задачи: `app/modules/jobs/store.py` (SKIP LOCKED, лизинг, `fire_due`), `runner.py`, `deadlines.py`.
- Уведомления:
  - `app/modules/notifications/{inbox,delivery,channels,fcm,device_push}.py`;
  - расширение — `ChannelRegistry.register(NotificationChannel)`: на узле канал `push` реализуется через
    rpc `push.send`.
- Telegram:
  - `app/modules/notifications/telegram.py` (`TelegramBot.handle_update` l.370);
  - webhook `app/api/v1/telegram.py`;
  - для бота на узле нужен цикл `getUpdates`, который вызывает тот же `handle_update`.

## Вход и сессии

- Android: `AuthService.exchange_google_token(id_token, nonce, device)` (`app/modules/auth/service.py:114`),
  refresh-семейства `auth_sessions`, access JWT HS256 (`tokens.py`). Облачный JWT узел проверить не может.
- «Войти через Mia»: `app/modules/oauth/` — OAuth 2.1, PKCE, ES256 ID-токены, JWKS (`idtoken.py`). Узел в режиме
  `home` — клиент этого сервера: CIMD на своём имени `https://<host>.n.<зона>/…`.
- Нужен порт `IdentityVerifier`. Источники личности:
  - Google — облако;
  - ID token Mia — узел в режиме `home`;
  - локальный pairing / passkey — `standalone`.
  Остальное (`RequestAuthenticator`, сессии, refresh-семейства) переиспользуется на узле как есть.

## Перенос и удаление

- Память: `app/modules/memory/transfer.py` (экспорт и импорт JSON/Markdown/zip) — основа `mia-archive/v1`.
- Удаление аккаунта: `app/modules/users/deletion.py` (M17-R1) — порядок «отозвать → уничтожить DEK → каскад».
  Переход в `home` использует ту же цепочку без удаления строки `users`.
- Ключи: `app/core/crypto.py`, `KeyVault` (M11) — DEK пользователя под мастер-ключом. На узле мастер-ключ хранится
  в хранилище ОС.

## Контракты

- `contracts/node/v1/**` — черновик M85, проверяется `validate_node()` в `contracts/scripts/validate_contracts.py`.
- Пути REST для узлов и коды ошибок добавляются в `contracts/mvp/v1` сессией M86, после слияния M84 (тот же файл
  `openapi.yaml`).
