# @mia/sdk (JavaScript / TypeScript)

Клиент для браузерных, мобильных и серверных приложений, которые входят через Mia («Войти через Мию»)
и вызывают модель пользователя. Пакет `@mia/sdk` 0.1.0, ES-модули, Node.js 22 и новее, браузеры
с `fetch`, `crypto.subtle` и `TextEncoder`. Зависимостей нет.

Пакет поставляется исходниками TypeScript (`exports` указывает на `src/index.ts`), в индекс не
опубликован. Подключайте его из репозитория как локальную зависимость и собирайте сборщиком с
поддержкой TypeScript или запускайте на Node 22 с `--experimental-strip-types` (OI-M43-003).

Вход, скоупы, сроки токенов и ошибки описаны в [быстром старте](../../docs/SDK_QUICKSTART.md).

## Пример

```ts
import { MiaClient, MiaOAuth } from '@mia/sdk'

const oauth = new MiaOAuth({
  issuer: 'https://mia.example',
  clientId: 'https://app.example.com/mia-client.json',
  redirectUri: 'https://app.example.com/callback',
})

const start = await oauth.start({ scope: 'openid llm.generate memory.context' })
// сохранить start.state и start.verifier до перехода на start.url
location.assign(start.url)

// страница callback
const tokens = await oauth.finish(location.href, { state: saved.state, verifier: saved.verifier })

const mia = new MiaClient({ baseUrl: 'https://mia.example', accessToken: tokens.access_token })
const reply = await mia.chatCompletion({ messages: [{ role: 'user', content: 'Привет' }] })
```

`accessToken` принимает строку или функцию (возвращает строку или Promise), если токен нужно
получать свежим при каждом запросе, например после обновления.

## Что экспортирует пакет

| Имя | Назначение |
| --- | --- |
| `MiaOAuth` | вход: `start({ scope, resource?, nonce? })`, `finish(callbackUrl, { state, verifier })`, `exchangeCode(code, verifier)`, `refresh(refreshToken, scope?)`, `revoke(token)`, `userinfo(accessToken)` |
| `createPkce()` | пара `{ verifier, challenge }` для S256 |
| `Tokens`, `SignInStart` | типы результатов входа |
| `MiaClient` | `chatCompletion(request)`, `streamChatCompletion(request)`, `response(request)`, `models()`, `createRun(text, profile?, key?)`, `cancelRun(id)`, `runEvents(id)`, `call(operationId, init)` |
| `readSse(body)` | разбор `text/event-stream` (комментарии и keep-alive пропускаются) |
| `MiaApiError`, `MiaOAuthError` | ошибки API (`status`, `code`) и входа (`error`, `description`) |
| `OPERATIONS`, `API_VERSION`, `OperationId`, `Operation` | таблица операций из контракта |

`chatCompletion` подставляет `model: 'mia:default'` (единственная доступная модель) и `stream: false`.
Потоковый вариант — `streamChatCompletion`, он отдаёт чанки в формате OpenAI.
`createRun`, `cancelRun` и `runEvents` рассчитаны на токены первого лица Мии; токены приложений здесь
не принимаются.

Refresh-токены ротируются: сохраняйте новый `refresh_token` из каждого `refresh(...)` сразу.

## Таблица операций

`src/operations.ts` генерируется из [openapi.yaml](../../contracts/mvp/v1/openapi.yaml) тем же
генератором, что и Python SDK: `uv run python sdk/python/scripts/generate_operations.py --check`
проверяет дрейф для обоих пакетов.

## Тесты

```bash
cd sdk/js
npm test   # node --test --experimental-strip-types tests/*.test.ts (Node 22, без зависимостей)
```

`tests/unit.test.ts` проверяет PKCE, URL входа, отказ при чужом `state` и `iss`, разбор SSE и
идемпотентность по таблице операций. `tests/e2e.ts` запускает `tests/oauth/test_sdk_postgres.py`
против живого API Мии (переменные `MIA_BASE_URL`, `MIA_ISSUER`). Проверка типов (`tsc -p tsconfig.json`)
требует установленный TypeScript: в `package.json` его нет.
