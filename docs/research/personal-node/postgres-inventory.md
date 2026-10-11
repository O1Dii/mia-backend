# Инвентаризация: что в ядре завязано на PostgreSQL и Redis

M85, 2026-10-11. Снимок `main` `61353a8`. Цель — оценить, сколько стоит запустить то же Python-ядро на узле дома
(Personal Node) на SQLite и сколько — на встроенном PostgreSQL. Числа получены поиском по `app/` и `migrations/`.
Файлы `.env*` не открывались.

## Размер

| Что | Объём |
|---|---|
| `app/` | 285 файлов `.py`, 50 870 строк |
| `migrations/versions/` | 18 ревизий, 1 983 строки, одна линейная цепочка |
| Таблицы ORM | 52 |
| `tests/` | 241 файл `.py`; около 78 тестовых файлов требуют Postgres (`MIA_TEST_DATABASE_URL`) |

## Подключение к БД

- Движок один: `app/core/database.py:18-19` (`create_async_engine(url, pool_pre_ping=True)`). URL везде берётся
  из `runtime.database`, поэтому подменить его можно в одном месте.
- `app/core/config.py:160-166` отвергает любой `DATABASE_URL`, кроме `postgresql+asyncpg://`.
- `core/database.py:34` и `workspace/legacy_import.py:189` вызывают `to_regclass(...)` — функцию, которая есть
  только в Postgres.
- Слоя репозиториев и unit of work нет. 53 файла сами принимают `AsyncSession`/`async_sessionmaker` и пишут
  запросы SQLAlchemy прямо в сервисах. Протоколы с in-memory-двойниками есть только местами: `RunStore`,
  `DeviceAccessStore`, `PluginStateStore`, `ApprovalService`, `ToolAudit`.

## Возможности Postgres, которые использует код

| Возможность | Мест | Где (главное) | Что нужно для SQLite |
|---|---|---|---|
| `insert().on_conflict_do_update/do_nothing` (диалект `postgresql`) | 27 в 14 файлах; 6 с `constraint="…"`, 2 с `index_where` | workspace, conversations, jobs, tools, auth, notifications, models | Хелпер диалект-нейтрального upsert; `constraint=` → `index_elements` |
| `with_for_update()` | 47 строк в 17 файлах | oauth 9, jobs 6, conversations/streaming 6, connections 5, memory 4 | SQLite молча игнорирует `FOR UPDATE`. Корректность держится на единственном писателе (`BEGIN IMMEDIATE`, WAL) — нужен аудит каждого места |
| `SKIP LOCKED` | 6 | `jobs/store.py:349,570,764`, `conversations/chats.py:697`, `notifications/delivery.py:61`, `notifications/inbox.py:277` | Не нужен при одном процессе-писателе; лизинг и ключи дедупликации уже обеспечивают корректность |
| Advisory lock | 1 | `auth/repositories.py:141` (ротация семейства refresh-токенов) | Заменить на блокировку строки или процесса |
| LISTEN/NOTIFY | 0 | — | — |
| Полнотекстовый поиск (`TSVECTOR` + GIN, `array_to_tsvector`, `@@`, `ts_rank`) | 1 модуль | `memory/models.py:56,75`, `memory/service.py:96-368` | Индекс построен по HMAC-токенам (blind index), а не по словам, поэтому заменяется таблицей токенов или FTS5 один к одному |
| JSONB-колонки | 31 (8 с `'{}'::jsonb` по умолчанию) | jobs 7, workspace 6, catalog 5, tools 4, runs 4 | `JSON` с вариантом под диалект; убрать `::jsonb` |
| JSONB-операторы и функции | ~8 | `jobs/store.py:153,269,272` (`\|\|`, `jsonb_array_length`), `jobs/deadlines.py:122` и `conversations/chats.py:708` (`jsonb_path_exists`, `::jsonpath`), `connections/resources.py:70` (`.astext`) | Переписать: `json_extract`, `json_each` или обработка в Python |
| `ARRAY` | 2 колонки | `memory/models.py:134`, `runs/models.py:65` | JSON-массив |
| `DISTINCT ON` | 2 | `plugins/catalog/service.py:283`, `conversations/chats.py:439` | Оконная функция или GROUP BY |
| `make_interval`, `btrim` | по 1 | `conversations/chats.py:683`, `conversations/feed.py:512` | Арифметику дат вынести в Python; `trim` |
| Частичные индексы | 13 (часть уникальные: главная лента, «одна основная модель») | conversations, jobs, models, notifications, runs, oauth | `sqlite_where` — поддерживаются |
| CHECK с regex `~` | 3 | `notifications/models.py:87,123,147` | Синтаксическая ошибка в SQLite: перенести проверку в Python |
| Нативные ENUM, `ALTER TYPE … ADD VALUE` | 7 колонок, ~14 мест в миграциях | conversations, tools | ORM на SQLite сам делает VARCHAR; миграции — нет |
| `RETURNING` | 27 | workspace, jobs и др. | SQLite ≥ 3.35 поддерживает |
| `DateTime(timezone=True)` | 110 колонок | везде | SQLite не хранит часовой пояс: нужен TypeDecorator «храним UTC, при чтении возвращаем aware», иначе Python падает на сравнении aware и naive |
| `ondelete=` (каскады) | 70 FK | на них держатся удаление аккаунта и retention | `PRAGMA foreign_keys=ON` на каждом соединении |

**Итого:** около 190 мест в ~27 файлах плюс миграции.

## Миграции

17 из 18 ревизий импортируют `sqlalchemy.dialects.postgresql`. На SQLite цепочка не пройдёт по нескольким
причинам:

- ENUM-типы и `ALTER TYPE`;
- `alter_column` и смена ограничений на существующих таблицах — без `render_as_batch`;
- приведения `::jsonb` и `::jsonpath`;
- JSONB-операторы `||`, `->`, `?`, `-` в data-миграциях;
- regex в CHECK;
- `TSVECTOR` + GIN.

Реалистичный путь для SQLite — отдельная базовая ревизия (схема из моделей) и своя ветка версий. Каждую будущую
миграцию тогда придётся писать и проверять дважды.

## Redis

Redis спрятан за протоколами (`AuthRedis`, `WebHandoffStore`/`HandoffRedis`, `CounterStore`). Клиент
`app/core/redis.py` реализует 4 операции:

- `set_if_absent`;
- `consume`;
- `increment_with_expiry`;
- `reserve_daily_budget`.

Через них работают одноразовые nonce входа и лимиты частоты, коды WebView hand-off и дневные бюджеты провайдера.
Pub/sub, очередей, SSE и блокировок на Redis нет: SSE — это in-process `asyncio.Event` плюс опрос БД.

**Цена:** in-process реализация тех же 4 методов, около 100 строк; кроме того, `redis_url` в конфиге и `Runtime`
нужно сделать необязательным. Размер S.

## Воркер

`python -m app.worker` — отдельный процесс с четырьмя циклами (`tick_jobs`, `tick_scheduler`, `tick_deadlines`,
`tick_deliveries`). Он берёт `JobsStack` из `create_app()`. Для узла достаточно запускать те же циклы в lifespan
API: один процесс, один писатель. Размер S.

## Тесты

Тесты с БД подключаются опционально через `MIA_TEST_DATABASE_URL`: Alembic `upgrade head`, затем
`TRUNCATE … CASCADE`. На SQLite не запускается ни один. Порт на SQLite означает вторую матрицу тестов — около
78 файлов harness/fixtures — и необходимость держать её зелёной всегда.

## Оценка двух вариантов

| | Встроенный PostgreSQL | SQLite |
|---|---|---|
| Изменения в коде ядра | Почти нет: `DATABASE_URL` указывает на локальный сервер, те же миграции; Redis → in-process (S); воркер в процессе API (S) | 1,5–3 тыс. строк в ~30 файлах + новая базовая миграция + TypeDecorator для времени + upsert-хелпер + FTS + аудит 47 мест с блокировками |
| Риск расхождения с облаком | Нет: тот же SQL, тот же `alembic upgrade head` | Каждая сессия пишет миграцию и запросы под два диалекта; ошибки проявятся только у пользователей узла |
| Тесты | Те же (Postgres) | Нужна вторая матрица |
| Упаковка | Бинарники Postgres для macOS (arm64/x86_64), Windows x86_64, Linux (x86_64/arm64), ~10–30 МБ в сжатом виде; `initdb` при первом запуске; процесс под присмотром `mia-node` | Ничего: SQLite встроен в Python |
| Эксплуатация у пользователя | Ещё один процесс (~30–60 МБ RAM без нагрузки); переход на новую мажорную версию Postgres — через выгрузку и загрузку архива (`mia-archive/v1`, нужен и для переноса облако ↔ узел); порт только на `127.0.0.1` или сокет | Один файл; бэкап — `VACUUM INTO` |
| Бэкап и перенос | `pg_dump` / логический архив | Копия файла / логический архив |
| Размер работы | **S–M** (упаковка и присмотр за процессом) | **L** (+ постоянная двойная поддержка) |

**Источник бинарников.**

- `pgserver` (pip) не подходит: последний выпуск 0.1.4 от июня 2024, Postgres 16.2, колёса только до CPython 3.12.
- Рабочий вариант — сборки `zonkyio/embedded-postgres-binaries`: Apache-2.0, Postgres 11–18, darwin/windows/linux,
  amd64/arm64v8, ~10 МБ. Плюс официальные сборки EDB для Windows.
- `mia-node` скачивает закреплённую версию с проверкой SHA-256 или везёт её в установщике. Мажорная версия совпадает
  с облаком (сейчас 16), чтобы архивы были совместимы.

**Рекомендация M85:** встроенный PostgreSQL для v1 (решение пользователя Р1 в ADR-0019). SQLite остаётся возможной
оптимизацией после пилота, если окажется, что отдельный процесс мешает (например, на слабых машинах).

Источники: [pgserver на PyPI](https://pypi.org/project/pgserver/),
[embedded-postgres-binaries](https://github.com/zonkyio/embedded-postgres-binaries).
