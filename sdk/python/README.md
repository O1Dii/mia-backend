# mia-sdk (Python)

Асинхронный клиент для приложений, которые входят через Mia («Войти через Мию») и вызывают модель
пользователя. Пакет `mia-sdk` 0.1.0, Python 3.11+, единственная обязательная зависимость — `httpx`.
Дополнительный extra `openai` ставит `openai`.

Пакет пока не опубликован в индекс: подключайте его из репозитория, например
`uv add ./sdk/python` или `pip install ./sdk/python` (OI-M43-003).

Вход, скоупы, сроки токенов и ошибки описаны в [быстром старте](../../docs/SDK_QUICKSTART.md).

## Пример

```python
from mia_sdk import MiaClient, MiaOAuth

oauth = MiaOAuth(
    issuer="https://mia.example",
    client_id="https://app.example.com/mia-client.json",
    redirect_uri="https://app.example.com/callback",
)
start = oauth.start(scope="openid llm.generate memory.context")   # перенаправить на start.url
tokens = await oauth.finish(callback_url, state=start.state, verifier=start.verifier)

async with MiaClient(base_url="https://mia.example", access_token=tokens.access_token) as mia:
    answer = await mia.chat_completions(
        {"model": "mia:default", "messages": [{"role": "user", "content": "Привет"}],
         "mia": {"context": True}}
    )
```

## Что экспортирует пакет

| Имя | Назначение |
| --- | --- |
| `MiaOAuth` | вход: `start(scope, resource=None, nonce=True)`, `finish(callback_url, state, verifier)`, `exchange_code(code, verifier)`, `refresh(refresh_token, scope=None)`, `revoke(token)`, `userinfo(access_token)` |
| `SignInStart`, `Tokens` | результат `start` (`url`, `state`, `verifier`, `nonce`) и токены (`access_token`, `refresh_token`, `expires_in`, `scope`, `id_token`) |
| `MiaClient` | `chat_completions(body)`, `stream_chat_completions(body)`, `responses(body)`, `models()`, `openai()` |
| `MiaClient.create_run`, `cancel_run`, `run_events` | запуски и поток событий; рассчитаны на токены первого лица Мии, токены приложений здесь не принимаются |
| `MiaClient.call(operation_id, ...)` | любая операция из `OPERATIONS` по её идентификатору |
| `MiaApiError`, `MiaOAuthError` | ошибки API (`status`, `code`, `message`) и входа (`error`, `description`) |
| `pkce_pair()` | пара `(verifier, challenge)` для S256 |
| `OPERATIONS`, `API_VERSION` | таблица операций, сгенерированная из контракта |

`chat_completions` и `responses` совпадают с OpenAI-совместимым API. Единственная доступная модель —
`mia:default` (модель, выбранная пользователем в Мии); `models()` возвращает её id.

Любая OpenAI-совместимая библиотека работает напрямую: `base_url="https://mia.example/v1"`,
`api_key=<access token>`, `model="mia:default"`. Для LiteLLM: `model="openai/mia:default"`.
`MiaClient.openai()` возвращает готовый `AsyncOpenAI` (нужен extra `openai`).

Refresh-токены ротируются: сохраняйте новый токен из каждого `oauth.refresh(...)` сразу.

## Таблица операций

`src/mia_sdk/_operations.py` генерируется из [openapi.yaml](../../contracts/mvp/v1/openapi.yaml):
путь, метод, требуемые скоупы и правило `Idempotency-Key` для операций тегов `Apps`, `Runs` и `Memory`.
Файл не редактируется вручную.

```bash
uv run python sdk/python/scripts/generate_operations.py          # перегенерировать
uv run python sdk/python/scripts/generate_operations.py --check  # проверка дрейфа (CI)
```

## Тесты

Проверки SDK входят в набор Mia: `uv run pytest tests/oauth -q` (нужен `MIA_TEST_DATABASE_URL`).
Там же тесты совместимости с OpenAI SDK и LiteLLM по реальному сокету.
