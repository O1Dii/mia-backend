# Мия Web

Web-клиент единого ядра Мии: чат на runs с голосовым вводом и озвучкой, подтверждения
действий, магазин плагинов, подключения моделей. Экраны используют токены и компоненты
дизайн-системы M33 (`src/styles/tokens.css`, `mia.css`); экраны M30 — `src/styles/app.css`
(префикс `mw-`). Контракты API: [`contracts/mvp/v1/openapi.yaml`](../contracts/mvp/v1/openapi.yaml).

## Экраны

| Путь | Что |
| --- | --- |
| `/chat` | Лента (`GET /v1/feed`), ответы на runs (`/v1/runs`, SSE с переподключением по `Last-Event-ID`), карточки `display`, шаги инструментов, подтверждения прямо в чате, голос |
| `/approvals` | Все ожидающие подтверждения (с телефона и web) и недавние решения |
| `/store`, `/store/plugin/<id>` | Каталог, страница плагина, экран согласия (инструменты с риском, разрешения), отзывы |
| `/store/connected` | Возврат после входа у провайдера (`?connection=ok\|error`) |
| `/store/add-url` | Добавить MCP-сервер по URL: проверка, вход, список инструментов, согласие |
| `/installed` | Установленные плагины: аккаунт, обновление с новым согласием, отключение, удаление |
| `/models` | Подключения моделей (`/v1/model-connections`): ключ виден только как `…last4` |

Голос: кнопка микрофона в поле ввода кладёт распознанный текст в поле (`SpeechRecognition`,
отправка вручную; опция «Отправлять сразу после диктовки»). У каждого ответа есть
«Озвучить/Остановить» (`speechSynthesis`). Если вопрос надиктован, `response.final.speech`
озвучивается сам; опция «Всегда озвучивать ответы». Опции хранятся в `localStorage`
(`mia.voice.prefs`). Браузер без распознавания: кнопка скрыта, показана подсказка.

## Запуск

Нужны Node.js 22.18+ и npm; публичный npm registry (CodeArtifact для этого репозитория не нужен).
Backend (из корня репозитория) — как обычно, с PostgreSQL и Redis; web-сессия — cookie
`mia_session`, поэтому web и API должны быть на одном origin (dev-сервер Vite проксирует `/v1`).

```sh
cd web
npm ci
MIA_API_URL=http://127.0.0.1:8000 npm run dev      # http://127.0.0.1:5173
```

Переменные сборки/запуска (значения не секретные):

| Переменная | Назначение |
| --- | --- |
| `VITE_GOOGLE_CLIENT_ID` | Web client ID Google для входа (публичный). Без него экран входа сообщает, что вход не настроен |
| `VITE_API_BASE` | Origin API, если он не совпадает с origin web (по умолчанию тот же origin) |
| `MIA_WEB_PORT`, `MIA_API_URL` | Порт Vite и адрес API для dev-прокси (`5173`, `http://127.0.0.1:8000`) |
| `MIA_BASE_PATH` | Префикс пути при публикации под подкаталогом |

OAuth подключений: backend должен знать внешний адрес (`PUBLIC_BASE_URL`), он же — origin web:
провайдер возвращает браузер на `<origin>/v1/connections/callback`, затем backend перенаправляет
на `/store/connected`. Привязка вызова к браузеру — Lax-cookie `mia_oauth_binding` (OI-M15-003).

## Проверка

```sh
npm run build          # tsc -b && vite build
npm run format:check   # Prettier
npx playwright install chromium
```

Playwright запускает настоящую сборку backend с поддельными внешними мирами
(`tests/support/e2e_server.py`: Google, модель, удалённый MCP-сервер и его сервер авторизации)
на одноразовых PostgreSQL и Redis. Из корня репозитория:

```sh
docker run -d --rm --name mia-web-pg -e POSTGRES_USER=mia -e POSTGRES_PASSWORD=mia-test \
  -e POSTGRES_DB=mia_test -p 55430:5432 postgres:16-alpine
cd web
MIA_TEST_DATABASE_URL=postgresql+asyncpg://mia:mia-test@localhost:55430/mia_test \
MIA_TEST_REDIS_URL=redis://localhost:6379/14 \
MIA_WEB_PORT=5230 MIA_API_PORT=8130 npm test
```

Тест очищает таблицу `users` и `plugin_releases` в этой базе — не указывайте рабочую БД.
Скриншоты экранов на 390/768/1440 px лежат в `test-results/screens/` (не коммитятся).

## Что осталось от прототипа

`src/lib/legacyApi.ts`, `lib/workspace.tsx`, `components/{Dashboard,PluginArt}.tsx`, `src/plugins/**`
и `scripts/` относятся к web-прототипу на `/api/web`; новые экраны их не используют (кроме
CSS-иллюстраций `PluginArt`). Дашборд, память и входящие переносит M31, он же удаляет остальное.
