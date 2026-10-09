# M01 — spike основной модели

Сравнивает два кандидата на «основную модель» MIA (Review §5.5):

| Провайдер | Что проверяется |
|---|---|
| `gpt-plan` | официальный Sign in with ChatGPT (SIWC): loopback + PKCE, регистрация клиента во время входа, refresh, Responses API (`store:false`, `stream:true`), function calling с продолжением, список моделей, заголовки лимитов, TTFT |
| `glm` | DeepInfra `zai-org/GLM-5.3-Flash` (OpenAI-совместимый chat completions): streaming, function calling, `reasoning_content`, TTFT, цена из публичного каталога |

Токены и ключи живут только в памяти процесса. В `results/*.json` пишутся
только санитизированные метрики (имена моделей, задержки, лимиты, булевы
проверки). Скрипт падает, если живой секрет попал в вывод.

## Запуск (в вашем терминале, не в чате)

```bash
cd /Users/aleksey/PycharmProjects/mia-helper-worktrees/M01   # worktree сессии M01

# 0. офлайн-проверка, без сети и секретов
uv run pytest spikes/m01/test_spike.py -p no:cacheprovider
uv run --script spikes/m01/run_spike.py --provider all --dry-run --port 0

# 1. GPT на плане ChatGPT (откроется браузер; войдите своим Plus и разрешите доступ)
uv run --script spikes/m01/run_spike.py --provider gpt-plan

# 2. GLM на DeepInfra (ключ спросит getpass — ввод не отображается)
uv run --script spikes/m01/run_spike.py --provider glm
#    либо заранее, без записи в историю shell:
#    read -rs "DEEPINFRA_API_KEY?DeepInfra key: "; export DEEPINFRA_API_KEY
```

Результаты: `spikes/m01/results/<provider>-<timestamp>.json`. После обоих
запусков сообщите «готово».

## Параметры

- `--runs N` — число замеров TTFT на модель (по умолчанию 5).
- `--model SLUG` — модель GPT (можно повторять); по умолчанию первая из
  `GET /v1/models` с `visibility: list`.
- `--glm-model ID` — по умолчанию `zai-org/GLM-5.3-Flash`.
- `--port N` — порт loopback-колбэка (по умолчанию 1455; если занят, например
  Codex CLI, берётся свободный).
- `--no-revoke` — не отзывать SIWC-доступ после прогона (по умолчанию
  refresh-токен отзывается, чтобы не оставлять висящих учётных данных).
- `--dry-run` — только фейки; результаты с суффиксом `-dry` игнорируются git.

## Что делает gpt-plan

1. `GET https://auth.openai.com/.well-known/openid-configuration` (публичные метаданные).
2. Слушатель на `http://127.0.0.1:<port>/auth/callback`, затем браузер на
   `/api/accounts/authorize` с `client_id=dynamic_agent_client`,
   `agent_name_hint`, `ext_agent_host_id` (`urn:uuid:…`), PKCE S256, `state`,
   `nonce`, `resource=https://api.openai.com/v1`, scope
   `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`.
3. Проверка `state`, обмен `code` на токены, проверка выданного `client_id`
   (`oaiapp_…`), `nonce`, `aud`, наличия `chatgpt.tokens.use.direct`.
   Подпись ID-токена в spike не проверяется (это обязанность M11 по JWKS).
4. Refresh (`grant_type=refresh_token`): проверяется ротация refresh-токена;
   дальше используется обновлённый access-токен.
5. `GET /v1/models`; на первой (или указанной) модели: `--runs` запросов
   «Say exactly: Hello, world!» и сценарий с tools `get_time` + `add_todo`
   (продолжение через `function_call_output`, при 400 — вторая попытка с
   `reasoning.encrypted_content`).
6. Отзыв сессии (`/oauth/revoke`).

Первый вход регистрирует клиент автоматически, заявка не нужна (документация
SIWC для open-source и локально размещённых приложений).
