# M01 — решение: основная модель MIA

**Решение: основная модель = GPT на плане ChatGPT (Sign in with ChatGPT, Responses API).
DeepInfra GLM-5.3-Flash остаётся проверенным запасным вариантом и generic OpenAI-compatible
адаптером.**

Дата прогона: 2026-10-09. Сырые данные (санитизированы):
`results/gpt-plan-20261009T014154Z.json`, `results/glm-20261009T014234Z.json`.
Условие Review §5.5 («все четыре пункта проходят → основная GPT») выполнено.

## Доказательства по пунктам §5.5

| # | Проверка | gpt-plan | glm |
|---|---|---|---|
| 1 | Регистрация клиента и вход своим Plus | PASS: `dynamic_agent_client` → выданный `oaiapp_…` в callback; `state`, `nonce`, `aud`, `iss` совпали; выдан scope `chatgpt.tokens.use.direct`; refresh PASS (refresh-токен ротируется, scope сохраняется); revoke PASS. Заявка/ожидание не потребовались | не применимо (ключ API) |
| 2 | Responses API `store:false`, `stream:true` | PASS на `gpt-6.1-sol`: 5/5 запросов дошли до `response.completed`, ошибок нет | chat completions stream 5/5 OK |
| 3 | Function calling (2 tools) с продолжением | PASS: `get_time` → `add_todo` → финальный текст; валидный JSON аргументов; продолжение в режиме `plain` (без `id` у `function_call`, без reasoning-items) сработало без повторной попытки с `encrypted_content` | PASS; `reasoning_content` приходит и проигрывается обратно в следующем ходе |
| 4 | Модели и лимиты | `GET /v1/models` → `{"models":[…]}`: 7 видимых (`gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`) и 3 скрытых. Числовые лимиты Plus **не наблюдаемы**: заголовки только `x-codex-turn-state`, `x-codex-safety-buffering-enabled`, `x-codex-safety-buffering-faster-model: gpt-6-luna` | заголовков лимитов нет; цена из каталога: $0.15 / $0.50 за 1M токенов in/out (cache read $0.03), контекст 1 048 576 |

## Задержка (мс, 5 запросов «Say exactly: Hello, world!»)

| | первое событие | первый токен (p50) | min–max | первый content-токен (p50) |
|---|---|---|---|---|
| gpt-6.1-sol (план) | 1340 | 2411 | 2364–4585 | 2411 |
| GLM-5.3-Flash | 1404 | 1405 (reasoning) | 1069–5492 | 2763 |

- В tool-сценарии первый токен на ход: GPT 2.3–2.7 с; GLM 0.8–3.1 с.
- GPT не выдаёт reasoning-дельт (`reasoning_seen=false`), GLM — выдаёт (`reasoning_content`).
  Для «первого полезного токена» (content) GPT быстрее: 2.4 с против 2.8 с p50; GLM быстрее
  только по reasoning-токену. Выборка мала (n=5), разброс большой — различие не значимо.
- Гипотеза, не проверена: `x-codex-safety-buffering-enabled: true` означает буферизацию потока;
  `faster-model: gpt-6-luna` намекает на более быструю модель. Прогон делался только на
  `gpt-6.1-sol` (первая в списке).

## Обоснование выбора

1. Все четыре проверки §5.5 проходят на официальном потоке; обходов и токенов Codex CLI нет.
2. Нулевая предоплата для разработки: используется собственный Plus владельца; GLM платный
   (оценка ~$0.00003 за ход tool-сценария — тоже дёшево).
3. Качество tool-calling и стабильность стрима достаточные (ошибок 0 из 8 стримов).
4. GLM остаётся «страховкой»: реализуется тем же generic OpenAI-compatible адаптером,
   который M11 всё равно делает; прогон тех же контрактных тестов переведёт его в «проверенные».

## Ограничения и риски (не блокируют выбор)

- **Лимиты Plus неизвестны численно** (общее окно с ChatGPT). Нужно обрабатывать
  `subscription_sharing_usage_limit_exceeded` (429 в `response.failed`) как «пауза» с
  понятным сообщением и переключением на запасного провайдера (OI-M01-001).
- **Условия:** план можно использовать только для подключённого приложения, без
  универсального API-доступа третьим сторонам. Документация охватывает open-source и локально
  размещённые приложения; платное/многопользовательское облако требует заявки OpenAI
  (interest form). Страница условий недоступна без браузера (403) — прочитать текст человеком
  (OI-M01-002).
- **Подпись ID-токена** в spike не проверялась; M11 обязан проверить через JWKS.
- Preview-статус: набор моделей, полей и ошибок может меняться без предупреждения.
- Проверена одна модель; сравнение моделей/задержек — OI-M01-003.

## Контракт для M11 (адаптер `chatgpt-plan`)

### OAuth (Authorization Code + PKCE, публичный клиент, без secret)

- Discovery: `https://auth.openai.com/.well-known/openid-configuration`; authorize
  `https://auth.openai.com/api/accounts/authorize`; token
  `https://auth.openai.com/api/accounts/oauth/token`; revoke
  `https://auth.openai.com/api/accounts/oauth/revoke`; JWKS
  `https://auth.openai.com/.well-known/jwks.json`.
- Первый вход: `client_id=dynamic_agent_client`, `agent_name_hint=<имя приложения>`,
  `ext_agent_host_id=urn:uuid:<uuid>` (создать и сохранить ДО первого входа; непрозрачный,
  уникальный на хост, не секрет). Повторный вход: сохранённый `oaiapp_…` без `agent_name_hint`.
- `redirect_uri=http://127.0.0.1:<port>/auth/callback` (только `127.0.0.1`; меняться может
  только порт; один и тот же URI в authorize и token). Слушатель стартует до открытия браузера.
  Слушатель должен быть многопоточным с таймаутами сокетов (браузер держит idle-соединения;
  однопоточный сервер блокируется — это был реальный баг spike).
- `response_type=code`, `scope=openid profile email offline_access resource.invoke
  chatgpt.tokens.use.direct`, `resource=https://api.openai.com/v1`, `state`, `nonce`,
  `code_challenge_method=S256`. Колбэк возвращает `code`, `state` и выданный `client_id`
  (сохранить; не сохранять `dynamic_agent_client`).
- Обмен: form POST `grant_type=authorization_code`, `client_id`, `code`, `code_verifier`,
  `redirect_uri`, `resource`. Проверить: `state`; ID-токен (подпись JWKS, `iss`, `aud=client_id`,
  `exp`, `nonce`); в `scope` есть `chatgpt.tokens.use.direct`.
- Токены: access ≈ 1 ч (`expires_in=3600`), refresh ≈ 30 дней и **ротируется** при каждом
  обновлении. Refresh: form POST `grant_type=refresh_token`, `client_id`, `refresh_token`,
  `resource` (без `scope`); сохранить новый refresh вместе с access атомарно; обновления
  сериализовать (lock). Обновлять за запас до `exp`. Ошибки `invalid_grant` /
  `refresh_token_reused` и т.п. → очистить токены, повторить вход с сохранённым `client_id`.
- Хранение: access/refresh шифрованно на сервере, только в памяти процесса или в зашифрованном
  хранилище M10; никогда в логах/ответах/events. Выход: revoke `refresh_token` (200 пусто).

### Запросы

- `POST https://api.openai.com/v1/responses`, заголовки `Authorization: Bearer <access>`,
  `Content-Type: application/json`. Не использовать ChatGPT `backend-api`.
- Тело: `model`, `instructions`, `input` (массив), `tools` (type `function`), `tool_choice`,
  `store:false`, `stream:true`. Не отправлять: `temperature`, `max_output_tokens`,
  `previous_response_id`, `metadata`, `user`, `top_p`, `truncation`, `background`,
  `conversation`, `prompt` и др. (см. Preview limitations). System-роль через `instructions`.
- Продолжение после tool call (проверено): добавить в `input` исходные сообщения,
  `{"type":"function_call","call_id","name","arguments"}` (без `id`), затем
  `{"type":"function_call_output","call_id","output":"<json>"}`; reasoning-items не нужны.
  Запасной режим: `include:["reasoning.encrypted_content"]` и проигрывание reasoning-items.
- Успех = событие `response.completed`. Парсить SSE: `response.output_text.delta`,
  `response.output_item.done` (item `function_call`: `call_id`, `name`, `arguments`),
  `response.function_call_arguments.delta`, `response.failed` (`response.error.code`).
- Модели: `GET /v1/models` → `{"models":[{"slug","visibility",…}]}`; брать `visibility=="list"`,
  значение `slug` — это `model`.
- Ошибки: 401 (креды/скоуп), 403 (`subscription_sharing_user_not_eligible`, регион),
  429 `subscription_sharing_usage_limit_exceeded` (пауза, не ретраить, предложить запасного
  провайдера), 503 (`…usage_unavailable`, `…user_unavailable`; ограниченный backoff),
  400 `subscription_sharing_unsupported_capability` (убрать параметр). Ошибки до стрима могут
  иметь форму `{"detail": "..."}`.
- Не поддерживаются: hosted tools (web search зависит от политики, image/file search,
  code interpreter, computer use, hosted MCP, `tool_search`); все tools — клиентские function.

### Запасной провайдер (GLM)

`POST https://api.deepinfra.com/v1/openai/chat/completions`, модель `zai-org/GLM-5.3-Flash`,
`stream:true`, `stream_options.include_usage`. В следующих ходах возвращать
`reasoning_content` ассистентского сообщения (проверено). Ключ — `DEEPINFRA_API_KEY`.

## Что ещё не доказано

- Численные лимиты Plus и поведение при их исчерпании (ни разу не достигнуты).
- Работа tool-calling на других моделях списка и их TTFT.
- Параллельные tool calls, длинные диалоги, изображения.
