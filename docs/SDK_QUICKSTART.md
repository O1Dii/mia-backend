# Быстрый старт: «Войти через Mia» и SDK

Обновлено 2026-10-09 (M43), по реализации M50. Приложение входит через Mia по OAuth 2.1
(authorization code + PKCE S256), получает токен пользователя и вызывает модель пользователя
через OpenAI-совместимый API. Ключи провайдеров модели приложению не нужны и не передаются.

Что получает приложение:

- вход пользователя без собственного пароля: Mia выдаёт токены доступа и обновления;
- `POST /v1/chat/completions`, `POST /v1/responses`, `GET /v1/models`: модель всегда `mia:default`
  (модель, которую пользователь выбрал в Мии);
- память по скоупу `memory.context` (сама память приложению не отдаётся, см. ниже);
- месячный лимит расходов на приложение; расход виден в `GET /v1/apps/{id}/usage`.

Схема API: [openapi.yaml](../contracts/mvp/v1/openapi.yaml), тег `Apps`. SDK:
[Python](../sdk/python/README.md), [JavaScript](../sdk/js/README.md). Решение о входе:
[ADR-0013](adr/0013-sign-in-with-mia.md).

## 1. Идентификатор приложения: Client ID Metadata Document

Публичное приложение без секрета описывает себя JSON-документом по https-адресу. Этот адрес и
есть `client_id`: документ должен содержать то же значение.

```json
{
  "client_id": "https://app.example.com/mia-client.json",
  "client_name": "Мой сервис",
  "redirect_uris": ["https://app.example.com/callback"],
  "grant_types": ["authorization_code"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

Правила, которые проверяет Mia:

- `client_id` в документе совпадает с URL документа; `client_secret` в документе запрещён;
- `token_endpoint_auth_method` только `none`; `authorization_code` и `code` обязательны;
- `redirect_uris`: от 1 до 10 адресов: `https`, либо `http` на loopback (`localhost`, `127.0.0.1`,
  RFC 8252), либо обратный DNS-схема для нативных приложений (например `com.example.app:/callback`);
- документ до 16 КБ, кэшируется (по умолчанию 1 час), загружается через защищённый сетевой клиент
  (публичные адреса, без приватных сетей).

Документ `client_id` с адресом `http://localhost` принимается только при `OAUTH_ALLOW_LOOPBACK_CLIENTS=true`
(локальная разработка). Эта настройка запрещена в staging и production.

## 2. Скоупы

Скоупы — строка через пробел. Неизвестный скоуп — ошибка `invalid_scope`. `offline_access`
принимается и игнорируется: refresh-токен выдаётся всегда.

| Скоуп | Что разрешает |
| --- | --- |
| `openid`, `profile`, `email` | вход и данные профиля (`/oauth/userinfo`); `openid` даёт `id_token` с парным `sub` для приложения |
| `llm.generate` | запросы к модели пользователя через `/v1/chat/completions` и `/v1/responses` |
| `memory.context` | Mia подмешивает уместную память к запросам приложения; факты в ответе не отдаются |
| `memory.suggest` | приложение может предложить факты; пользователь подтверждает сам |
| `memory.read:<категория>` | прямое чтение фактов категории: `profile`, `work`, `home`, `interests`, `other` |
| `tools.use:<plugin_id>` | инструменты установленного плагина, например `tools.use:team.linear` |
| `jobs`, `notify`, `mia.install` | можно выдать, но эндпоинтов пока нет (OI-M50-002) |

Пользователь видит скоупы на экране согласия и может сузить категории памяти и лимит расходов.
Категории здоровья и финансов в `memory.context` не попадают никогда.

## 3. Python

```python
from mia_sdk import MiaClient, MiaOAuth

oauth = MiaOAuth(
    issuer="https://mia.example",
    client_id="https://app.example.com/mia-client.json",
    redirect_uri="https://app.example.com/callback",
)

# Начало входа: сохраните state и verifier в сессии пользователя и перенаправьте на start.url
start = oauth.start(scope="openid llm.generate memory.context")

# Обратный вызов: проверяет state и iss, обменивает код на токены
tokens = await oauth.finish(callback_url, state=start.state, verifier=start.verifier)
# сохраните tokens.refresh_token; access_token живёт около 15 минут

async with MiaClient(base_url="https://mia.example", access_token=tokens.access_token) as mia:
    reply = await mia.chat_completions(
        {
            "model": "mia:default",
            "messages": [{"role": "user", "content": "Привет"}],
            "mia": {"context": True},
        }
    )
    print(reply["choices"][0]["message"]["content"])
```

Обновление токена: `tokens = await oauth.refresh(saved_refresh_token)`. Refresh-токен ротируется:
новый нужно сохранить сразу, старый больше не работает.

## 4. JavaScript

```ts
import { MiaClient, MiaOAuth } from '@mia/sdk'

const oauth = new MiaOAuth({
  issuer: 'https://mia.example',
  clientId: 'https://app.example.com/mia-client.json',
  redirectUri: 'https://app.example.com/callback',
})

// Начало входа: state и verifier нужны до возврата из Mia
const start = await oauth.start({ scope: 'openid llm.generate memory.context' })
sessionStorage.setItem('mia-signin', JSON.stringify(start))
location.assign(start.url)

// На странице callback
const saved = JSON.parse(sessionStorage.getItem('mia-signin') ?? '{}')
const tokens = await oauth.finish(location.href, { state: saved.state, verifier: saved.verifier })

const mia = new MiaClient({ baseUrl: 'https://mia.example', accessToken: tokens.access_token })
const reply = await mia.chatCompletion({
  messages: [{ role: 'user', content: 'Привет' }],
  mia: { context: true },
})
```

`verifier` хранится только на время входа и удаляется после `finish`. Приложение публичное,
секрета у него нет, поэтому защиту обеспечивает PKCE.

## 5. Без SDK: OpenAI-совместимый API

Любая библиотека, которая принимает `base_url`, работает с Mia:

```bash
curl https://mia.example/v1/chat/completions \
  -H "Authorization: Bearer $MIA_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"mia:default","messages":[{"role":"user","content":"Привет"}],"mia":{"context":true}}'
```

- OpenAI Python: `OpenAI(base_url="https://mia.example/v1", api_key=access_token)`, `model="mia:default"`.
- LiteLLM: `model="openai/mia:default"`, `api_base="https://mia.example/v1"`, `api_key=access_token`.

Расширение `mia` в теле запроса: `context` (по умолчанию `true`), `tools` (список id плагинов или `"*"`),
`budget: {max_usd}`. В ответе — `mia: {run_id, cost_usd, cost_estimated, budget_exceeded?}`. Если
провайдер не сообщает цену, расход оценивается по токенам, и поле `cost_estimated` равно `true`.

## 6. Срок жизни и лимиты

| Что | Значение по умолчанию | Настройка |
| --- | --- | --- |
| Access-токен | 15 минут | `OAUTH_ACCESS_TOKEN_TTL_SECONDS` |
| Refresh-токен | 30 дней, ротируется; повтор старого отзывает всю цепочку | `OAUTH_REFRESH_TOKEN_TTL_DAYS` |
| Код авторизации | 60 секунд, одноразовый | `OAUTH_CODE_TTL_SECONDS` |
| Запрос на согласие | 10 минут | `OAUTH_REQUEST_TTL_SECONDS` |
| Месячный лимит приложения | 5 USD, максимум 100 USD | `APPS_DEFAULT_MONTHLY_LIMIT_USD`, `APPS_MAX_MONTHLY_LIMIT_USD` |

Токены — непрозрачные строки `mia_at_…` (доступа) и `mia_rt_…` (обновления); хранятся в Mia
только в виде SHA-256. Токены приложения не принимаются в собственных эндпоинтах Мии (`/v1/runs`,
`/v1/approvals` и др.).

## 7. Ошибки

Ответы об ошибках имеют код, например `invalid_token` (401), `insufficient_scope` (403, с
заголовком `WWW-Authenticate`), `app_budget_exceeded` (429, `x-should-retry: false`),
`unsupported_model` (400), `validation_error` (422). Полный список — в
[error-codes.json](../contracts/mvp/v1/error-codes.json).

- Python: `MiaApiError(status, code, message)` для API, `MiaOAuthError(error, description)` для входа.
- JavaScript: `MiaApiError` и `MiaOAuthError` с такими же полями `status`/`code` и `error`/`description`.

Если API вернул `invalid_token`, обновите токен через refresh-токен. Если refresh тоже отклонён,
пользователь должен войти заново.

## 8. Отзыв доступа

- Пользователь отзывает приложение в «Приложения и доступ»; тогда его токены перестают работать.
- Приложение может отозвать свой токен: `POST /oauth/revoke` (RFC 7009) с полями `token` и `client_id`.
  Python: `oauth.revoke(token)`, JavaScript: `oauth.revoke(token)`.

## 9. Что ещё не работает

- Возврат на экран согласия после входа в web (OI-M50-001).
- Фоновые задачи, уведомления и установка плагинов для приложений (OI-M50-002).
- Токены приложений на `/v1/runs` и `/v1/memory/context` (OI-M50-003).
- Бюджет при параллельных запросах может быть превышен на один шаг модели на запрос (OI-M50-004).
- Issuer с путём, например `https://host/mia`, требует правил nginx (OI-M50-005).

## Связанные документы

- [Руководство по плагинам](PLUGIN_DEVELOPMENT.md) — если нужен инструмент или плитка, а не вход.
- [Python SDK](../sdk/python/README.md), [JavaScript SDK](../sdk/js/README.md).
- [ADR-0013: Sign in with Mia](adr/0013-sign-in-with-mia.md).
- [Handoff M50](implementation/handoffs/M50.md): реализация, проверки и решения.
