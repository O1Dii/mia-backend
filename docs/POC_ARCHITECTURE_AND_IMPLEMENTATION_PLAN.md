# MIA: архитектура и план реализации Android-приложения и backend

Статус: проектное решение для PoC  
Дата: 2026-09-10  
Исходный документ: InitialPlan.md

Короткая точка запуска находится в `session_plan.md`: достаточно команды `Выполни Sxx`. Детальный план автономных реализационных сессий, handoff contract и общий журнал вопросов находятся в `docs/implementation/SESSION_EXECUTION_PLAN.md`.

## 1. Резюме решения

PoC строится как единая conversational-платформа, у которой Android-приложение является первым клиентом. Backend хранит пользователей, разговоры, историю, правила инструментов и аудит. Gemini временно предоставляет text generation и realtime speech-to-speech, но скрыт за внутренними интерфейсами провайдера.

Первый полезный вертикальный срез:

1. Вход через Google.
2. Единый текстовый чат с потоковой выдачей и историей.
3. Голосовой режим по нажатию: микрофон → backend WebSocket → Gemini Live → потоковое аудио.
4. Barge-in: пользователь начинает говорить, текущий ответ немедленно останавливается.
5. Один read-only tool, например погода, чтобы доказать полный цикл function calling.
6. После стабилизации этого пути — локальная wake phrase и интеграция с ролью системного ассистента.

Основные решения:

- Android: Kotlin + Jetpack Compose, нативный audio pipeline.
- Backend: Python + FastAPI, модульный монолит.
- Основное хранилище: PostgreSQL.
- Эфемерное состояние: Redis.
- Text transport: REST + SSE.
- Voice transport: один provider-neutral WebSocket между Android и backend.
- AI PoC: Gemini Text API и Gemini Live API через Google Gen AI SDK.
- Google OAuth используется только для идентификации пользователя; после входа backend выдаёт собственную сессию.
- API-ключ Gemini никогда не попадает в APK.
- Сырое аудио по умолчанию не сохраняется.
- Текст, голос и будущие клиенты используют одну модель Conversation/Turn/Entry.
- Микросервисы, Kafka, Kubernetes, marketplace, физическая колонка и локальная LLM не входят в PoC.

## 2. Границы PoC

### 2.1 Цель

Проверить, что пользователь может ежедневно применять MIA на Android как единый текстовый и голосовой ассистент, а архитектура позволяет заменить Gemini локальным inference без переделки клиента и доменной модели.

### 2.2 Пользовательские сценарии

Обязательные:

- войти через Google;
- создать новый разговор;
- написать сообщение и видеть потоковый ответ;
- закрыть приложение, открыть снова и увидеть историю;
- нажать микрофон, произнести запрос и услышать потоковый ответ;
- увидеть транскрипт голосового запроса и ответа в том же разговоре;
- перебить ассистента голосом и сразу остановить воспроизведение;
- получить ответ с вызовом read-only tool;
- повторить запрос после краткого обрыва сети без дублирования сообщения.

Следующий слой PoC:

- локальная активационная фраза в foreground;
- запрос роли ASSISTANT;
- запуск VoiceInteractionSession системным жестом;
- эксперимент с background hotword на нескольких OEM-устройствах.

### 2.3 Не входит в PoC

- физическая колонка;
- локальные STT, LLM и TTS;
- Telegram, Web и iOS;
- сторонний Plugin SDK и marketplace;
- пользовательские файлы и RAG;
- платежи и подписки;
- семейные профили и speaker recognition;
- полноценная долговременная память;
- музыка, Home Assistant и опасные write-tools;
- Kubernetes, Kafka, отдельные AI worker pools.

Эти функции учитываются в границах модулей, но не реализуются заранее.

## 3. Критерии успеха

Функциональные:

- один conversation содержит вперемешку text и voice turns;
- после завершения voice turn оба транскрипта доступны в истории;
- tool call проходит через backend registry и сохраняется в аудите;
- клиент не знает API Gemini и не содержит его секретов;
- смена fake/Gemini provider выполняется конфигурацией backend;
- повтор запроса с тем же idempotency key не создаёт второй turn.

UX и производительность:

- text time-to-first-token: целевой p50 до 1 секунды, p95 до 2,5 секунды;
- voice end-of-utterance → first audible response: целевой p50 до 1,5 секунды, p95 до 3 секунд;
- реакция на barge-in и очистка playback buffer: до 200 мс на исправном устройстве;
- отсутствие заметных щелчков между выходными audio chunks;
- reconnect после краткого сетевого разрыва не теряет уже завершённые turns;
- приложение корректно работает минимум на одном Pixel/AOSP и одном Samsung-устройстве.

Качество:

- backend unit и integration tests проходят локально и в CI;
- Android reducer/state-machine и repositories покрыты unit tests;
- основной E2E happy path проверяется с FakeAIProvider без внешнего API;
- smoke suite отдельно запускается с Gemini;
- секреты не находятся в git, логах, crash reports и Android resources.

## 4. Структура репозиториев

Текущий репозиторий остаётся backend:

~~~text
/Users/aleksey/PycharmProjects/mia-helper
~~~

Android следует создать отдельным соседним репозиторием в момент начала реализации:

~~~text
/Users/aleksey/PycharmProjects/mia-helper-android
~~~

Будущие клиенты также должны быть отдельными deployable units:

~~~text
mia-helper-speaker
mia-helper-web
~~~

Контракт между репозиториями публикуется из backend:

- OpenAPI для REST;
- отдельная JSON Schema/Markdown-спецификация realtime events;
- набор записанных contract fixtures;
- версия протокола в каждом realtime handshake.

Monorepo сейчас не даёт заметного выигрыша: Android и backend имеют разные toolchains, release cycles и способы доставки.

## 5. Контекстная архитектура

~~~text
┌──────────────────────── Android app ────────────────────────┐
│ Compose UI  Credential Manager  Room  Audio/Wake-word core  │
└───────────────┬──────────────────────────┬───────────────────┘
                │ HTTPS REST/SSE           │ WSS control + PCM
                ▼                          ▼
┌────────────────────────── Backend ───────────────────────────┐
│ API/Auth │ Conversation │ Realtime │ Orchestrator │ Tools   │
│          │              │          │              │ Policy  │
└──────┬───────────┬───────────────┬───────────────┬──────────┘
       │           │               │               │
       ▼           ▼               ▼               ▼
 PostgreSQL      Redis       GeminiAdapter     External tools
                                │
                     ┌──────────┴──────────┐
                     │ Text API   Live API │
                     └─────────────────────┘
~~~

Backend является control plane и data plane PoC:

- проверяет пользователя;
- создаёт разговор и turn;
- проксирует realtime audio;
- объявляет модели только разрешённые tools;
- исполняет и валидирует tool calls;
- сохраняет транскрипты, статусы и usage;
- применяет rate limits и лимиты стоимости.

Такой relay добавляет один сетевой hop, но даёт для PoC более важные свойства: единый аудит, отсутствие бизнес-логики на клиенте, простой provider swap и безопасное исполнение tools.

## 6. Основные потоки

### 6.1 Google sign-in

~~~text
Android              Backend                 Google
   │ POST nonce         │                       │
   │───────────────────>│                       │
   │ nonce              │                       │
   │<───────────────────│                       │
   │ Credential Manager ├──────────────────────>│
   │ Google ID token    │<──────────────────────┤
   │ POST id_token      │                       │
   │───────────────────>│ verify signature,     │
   │                    │ aud, iss, exp, nonce  │
   │ app access +       │                       │
   │ refresh tokens     │                       │
   │<───────────────────│                       │
~~~

Правила:

- Android использует Web OAuth Client ID как serverClientId.
- ID token всегда проверяется на backend.
- Идентификатором внешней учётной записи служит claim sub, а не email.
- Nonce одноразовый, хранится в Redis с коротким TTL.
- Backend выпускает собственный short-lived access token и rotating opaque refresh token.
- Refresh token хранится на сервере только в виде хэша.
- На Android refresh token хранится с защитой Android Keystore.

GOOGLE_CLIENT_ID нужен Android-приложению как публичный audience и backend для проверки aud. GOOGLE_CLIENT_SECRET нельзя помещать в Android; для простого ID-token sign-in он обычно не нужен и понадобится только для отдельных server-side OAuth flows. Для вызова Gemini потребуется отдельная backend-настройка GEMINI_API_KEY либо эквивалентный provider credential.

### 6.2 Текстовый turn

~~~text
Android → POST message:stream → ConversationService
        → Orchestrator → GeminiTextProvider
        ← token/tool events через SSE
        → сохранение user/assistant/tool entries
        ← message.completed
~~~

До обращения к модели user entry сохраняется со статусом pending/processing. Это позволяет безопасно повторить оборванный HTTP-запрос. После завершения assistant entry получает status=completed. При ошибке turn остаётся видимым со status=failed и может быть повторён.

### 6.3 Голосовой turn

~~~text
Android                    Backend                   Gemini Live
   │ create realtime          │                          │
   │─────────────────────────>│                          │
   │ session_id + ws_url      │                          │
   │<─────────────────────────│                          │
   │ WSS + bearer token       │ connect/config/tools     │
   │─────────────────────────>│─────────────────────────>│
   │<──── session.ready ──────│<──── setup complete ─────│
   │ binary PCM 16 kHz        │ realtime input           │
   │─────────────────────────>│─────────────────────────>│
   │                         transcript deltas           │
   │<─────────────────────────│<──────────────────────────│
   │                          │<──── function call ───────│
   │                          │ validate/execute/audit    │
   │                          │──── function result ─────>│
   │ binary PCM 24 kHz        │<──── response audio ─────│
   │<─────────────────────────│                          │
   │ transcript final         │ persist completed turn   │
   │<─────────────────────────│                          │
~~~

### 6.4 Barge-in

1. Android продолжает локально анализировать микрофон во время playback.
2. Локальный VAD подтверждает начало речи.
3. Android немедленно останавливает и очищает AudioTrack.
4. Клиент отправляет response.cancel/interruption и продолжает audio input.
5. Backend отменяет pending output, прекращает пересылку устаревших chunks и отмечает ответ interrupted.
6. Gemini Live получает новую активность и отменяет текущую генерацию.
7. Уже произнесённая часть остаётся в истории, незавершённая часть не считается доставленной.

Каждый audio/control event имеет monotonically increasing sequence. Backend и Android игнорируют chunks старого response_id после отмены.

### 6.5 Wake phrase

Wake-word detector ничего не меняет в серверном протоколе. Он только заменяет tap-to-talk как trigger:

~~~text
local PCM ring buffer → KWS detects phrase → chime
→ открыть voice session → отправить pre-roll + речь после trigger
~~~

До активации аудио не отправляется на backend. Для первого этапа wake phrase работает только при видимом приложении. Background-вариант добавляется после отдельного Android capability spike.

## 7. Backend

### 7.1 Стек

- Python 3.12+;
- FastAPI и Uvicorn;
- Pydantic для API/config/tool schemas;
- SQLAlchemy 2 async;
- Alembic;
- PostgreSQL;
- Redis;
- официальный Google Gen AI SDK;
- HTTPX для внешних tools;
- pytest, pytest-asyncio, Ruff и статическая проверка типов;
- Docker Compose для local environment.

Версии фиксируются lockfile. Конкретные Gemini model IDs не зашиваются в код, потому что Live-модели могут быть preview и меняться.

### 7.2 Модульный монолит

~~~text
app/
  main.py
  api/
    dependencies.py
    errors.py
    v1/
  core/
    config.py
    database.py
    redis.py
    logging.py
    security.py
    telemetry.py
  modules/
    auth/
      api.py
      service.py
      repository.py
      models.py
      schemas.py
    users/
    conversations/
      api.py
      service.py
      repository.py
      models.py
      schemas.py
    realtime/
      api.py
      protocol.py
      session.py
      state_machine.py
    orchestration/
      service.py
      context_builder.py
      prompt.py
    tools/
      registry.py
      executor.py
      policy.py
      schemas.py
      builtin/
    usage/
  providers/
    ai/
      base.py
      fake.py
      gemini_text.py
      gemini_live.py
    weather/
  migrations/
tests/
  unit/
  integration/
  contract/
  e2e/
docs/
docker-compose.yml
pyproject.toml
.env.example
~~~

API layer не обращается к SQLAlchemy или Gemini напрямую. Направление зависимостей:

~~~text
API/WS → application services → domain ports → provider/repository adapters
~~~

### 7.3 Интерфейсы AI provider

Концептуальные порты:

~~~text
TextAIProvider
  stream(request) -> AsyncIterator[AIEvent]

RealtimeAIProvider
  open(config) -> RealtimeProviderSession

RealtimeProviderSession
  send_audio(chunk)
  commit_audio()
  send_tool_result(result)
  cancel_response()
  events() -> AsyncIterator[AIEvent]
  close()
~~~

Нормализованные AIEvent:

- TextDelta;
- InputTranscriptDelta/Final;
- OutputTranscriptDelta/Final;
- AudioDelta;
- ToolCallRequested;
- ToolCallCancelled;
- ResponseStarted/Completed/Interrupted;
- UsageReported;
- ProviderError.

Ни ConversationService, ни Android-протокол не импортируют типы Google SDK. В будущем GeminiAdapter заменяется цепочкой LocalSTTAdapter + LocalLLMAdapter + LocalTTSAdapter, которая выдаёт те же события.

### 7.4 Gemini PoC adapter

Text:

- отдельная настраиваемая Flash-модель;
- streaming output;
- manual function-call loop;
- server-side system instruction;
- таймауты и max output tokens;
- usage metadata сохраняется.

Voice:

- backend открывает Gemini Live WebSocket;
- вход: raw PCM signed 16-bit little-endian mono, предпочтительно 16 kHz;
- выход Gemini: PCM 24 kHz;
- включены input и output audio transcription;
- включён server VAD, а Android посылает audio_stream_end по локальному end-of-speech;
- response audio пересылается без ожидания полного ответа;
- при interrupted backend немедленно прекращает старый output stream;
- session resumption handle хранится в Redis только на время активного разговора;
- соединение заранее закрывается или возобновляется с учётом лимита Live-сессии.

Конфигурация:

~~~text
AI_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_TEXT_MODEL=...
GEMINI_LIVE_MODEL=...
GEMINI_REQUEST_TIMEOUT_SECONDS=...
GEMINI_MAX_CONCURRENT_LIVE_SESSIONS=...
~~~

Model ID выбирается через environment и проверяется startup smoke test. Preview-модель не должна фигурировать в domain code или Android.

### 7.5 Контекст разговора

ContextBuilder формирует запрос из:

1. стабильной system instruction с правилами языка, safety и tools;
2. краткого summary старой части разговора;
3. последних N entries в token budget;
4. минимальных profile facts: locale, timezone;
5. результатов разрешённых tools текущего turn.

PoC не делает автоматическую долговременную память. Summary обновляется после достижения порога, но исходные entries остаются canonical history.

Нельзя полагаться только на контекст внутри Gemini Live session: после reconnect, смены провайдера или продолжения текстом backend должен восстановить смысл из своей базы.

### 7.6 Tool subsystem

Каждый tool описан сервером:

~~~text
ToolDefinition
  name
  description
  JSON/Pydantic input schema
  output schema
  risk: read | write | sensitive
  timeout
  required scopes
  requires_confirmation
~~~

Путь вызова:

~~~text
model proposal
→ registry lookup
→ schema validation
→ permission/policy check
→ optional confirmation
→ execution with timeout
→ sanitized result
→ audit record
→ result to model
~~~

Модель никогда не вызывает Python-функцию автоматически без policy layer. Даже если SDK предлагает automatic function calling, в MIA используется явный manual loop.

Первый tool — read-only и детерминированный по схеме:

- weather.get_forecast, либо
- system.get_current_datetime.

Затем можно добавить reminder.create как первый confirmation-required write-tool. Для всех мутаций обязателен idempotency key, чтобы retry не создал два напоминания.

### 7.7 Ошибки

Единый REST error envelope:

~~~json
{
  "error": {
    "code": "realtime_provider_unavailable",
    "message": "Voice service is temporarily unavailable",
    "request_id": "...",
    "retryable": true
  }
}
~~~

Пользователю не возвращаются stack trace, provider payload или секреты. Provider errors переводятся в стабильные MIA-коды.

## 8. Realtime protocol

### 8.1 Handshake

1. Android делает POST /v1/realtime/sessions.
2. Backend проверяет quota и создаёт короткоживущую session.
3. Android открывает WSS с Authorization: Bearer.
4. Первое сообщение клиента содержит protocol_version, conversation_id, locale и audio capabilities.
5. Backend отвечает session.ready с выбранными input/output formats.
6. Только после ready клиент отправляет binary frames.

### 8.2 Формат

Control frames — UTF-8 JSON text. Audio frames — WebSocket binary:

- Android → backend binary означает input PCM;
- backend → Android binary означает output PCM;
- response_id и sequence объявляются control frame до audio segment;
- максимальный размер chunk ограничен;
- unbounded queues запрещены.

Рекомендуемый входной chunk для интерактивного режима: 20–40 мс. При 16 kHz, mono, PCM16 chunk 40 мс равен 1280 bytes. Если сеть или SDK показывают худший overhead, размер настраивается и измеряется, а не меняется в контракте.

### 8.3 Client events

~~~text
session.configure
input_audio.start
input_audio.commit
response.cancel
session.close
ping
~~~

Аудио передаётся binary frames между input_audio.start и commit.

### 8.4 Server events

~~~text
session.ready
input_audio.speech_started
input_audio.speech_stopped
transcript.input.delta
transcript.input.final
response.started
response.audio.started
response.audio.completed
transcript.output.delta
transcript.output.final
tool.call.started
tool.call.completed
turn.completed
response.interrupted
usage.updated
error
pong
~~~

Каждый event содержит:

~~~json
{
  "v": 1,
  "type": "response.interrupted",
  "session_id": "...",
  "turn_id": "...",
  "response_id": "...",
  "seq": 42,
  "at": "RFC3339 timestamp",
  "payload": {}
}
~~~

### 8.5 Backpressure и reconnect

- Android держит небольшой bounded input queue, ориентир 200–300 мс.
- При переполнении клиент сообщает метрику и завершает/restarts turn; он не копит секунды устаревшего audio.
- Backend имеет отдельные bounded queues для client→provider и provider→client.
- Медленный потребитель приводит к контролируемому session error, а не росту памяти.
- Завершённые turns восстанавливаются из PostgreSQL.
- Незавершённый turn после потери соединения помечается interrupted.
- Provider session resumption используется как оптимизация, но не как единственный источник контекста.
- Ping/pong обнаруживает half-open connection.

## 9. REST API v1

Минимальный контракт:

~~~text
POST   /v1/auth/google/nonce
POST   /v1/auth/google
POST   /v1/auth/refresh
POST   /v1/auth/logout
GET    /v1/me

POST   /v1/conversations
GET    /v1/conversations?cursor=...
GET    /v1/conversations/{id}
PATCH  /v1/conversations/{id}
DELETE /v1/conversations/{id}
GET    /v1/conversations/{id}/entries?cursor=...
POST   /v1/conversations/{id}/messages:stream

POST   /v1/realtime/sessions
DELETE /v1/realtime/sessions/{id}
WS     /v1/realtime

POST   /v1/tool-calls/{id}/confirm
POST   /v1/tool-calls/{id}/reject

GET    /health/live
GET    /health/ready
~~~

Требования:

- cursor pagination, не offset;
- UUID/ULID идентификаторы, генерируемые сервером;
- Idempotency-Key для создания turn/session и всех мутаций;
- request_id во всех ответах и логах;
- UTC в базе, timezone пользователя применяется при отображении и tools;
- OpenAPI является проверяемым артефактом CI.

## 10. Модель данных

### users

- id;
- google_sub, unique;
- email и email_verified;
- display_name, avatar_url;
- locale, timezone;
- created_at, updated_at, deleted_at.

### auth_sessions

- id, user_id, device_id;
- refresh_token_hash;
- expires_at, rotated_at, revoked_at;
- created_ip_hash/user_agent metadata по минимально необходимой политике.

### devices

- id, user_id;
- platform, app_version, device_name;
- push_token позже;
- capabilities JSON;
- last_seen_at.

### conversations

- id, user_id;
- title;
- summary;
- next_sequence;
- created_at, updated_at, archived_at.

### turns

- id, conversation_id;
- source: android_text | android_voice | future clients;
- status: pending | active | completed | interrupted | failed;
- idempotency_key;
- started_at, completed_at;
- latency metadata JSON.

### entries

- id, conversation_id, turn_id;
- sequence;
- role: user | assistant | tool | system;
- kind: text | transcript | tool_call | tool_result | artifact_ref;
- modality: text | audio;
- content JSONB;
- delivery status;
- provider metadata без секретов;
- created_at.

### tool_executions

- id, turn_id, tool_name;
- input JSONB;
- risk;
- state: proposed | waiting_confirmation | running | succeeded | failed | cancelled;
- output/error JSONB с redaction;
- idempotency_key;
- timestamps.

### realtime_sessions

- id, user_id, conversation_id;
- status;
- provider, provider_session_reference;
- connected_at, disconnected_at;
- aggregate usage/latency metadata.

### usage_events

- user_id, turn_id, provider/model alias;
- input/output tokens по modality;
- duration;
- estimated cost;
- created_at.

Индексы:

- users(google_sub);
- conversations(user_id, updated_at desc);
- entries(conversation_id, sequence);
- turns(conversation_id, started_at);
- tool_executions(turn_id);
- auth_sessions(user_id, revoked_at);
- уникальный ключ на user/endpoint/idempotency_key.

Доступ к conversation всегда фильтруется одновременно по conversation_id и authenticated user_id.

## 11. Android-приложение

### 11.1 Стек

- Kotlin;
- Jetpack Compose + Material 3;
- Coroutines и Flow;
- Credential Manager + Sign in with Google;
- OkHttp для REST/SSE/WebSocket;
- kotlinx.serialization;
- Room для локального cache истории;
- DataStore для несекретных настроек;
- Android Keystore-backed storage для refresh token;
- Hilt или другой один DI container;
- Media3 только если позже потребуется сложный media playback.

### 11.2 Модули

~~~text
:app
:core:model
:core:network
:core:database
:core:auth
:core:audio
:core:designsystem
:feature:auth
:feature:conversation-list
:feature:chat
:feature:voice
:feature:settings
:feature:assistant
~~~

Для первого commit допустимо объединить мелкие core-модули, но граница audio должна быть отдельной: она сложнее UI и позже будет переиспользована в VoiceInteractionService.

### 11.3 Слои

~~~text
Compose screen
→ ViewModel / unidirectional state
→ use case
→ repository
→ REST/SSE/WS or Room
~~~

UI не управляет AudioRecord/AudioTrack напрямую. VoiceSessionController владеет voice state machine и публикует StateFlow.

### 11.4 Voice state machine

~~~text
Idle
→ Connecting
→ Ready
→ Listening
→ UserSpeaking
→ WaitingForResponse
→ AssistantSpeaking
→ Interrupting
→ Listening
→ Closing

Любое состояние → RecoverableError | FatalError
~~~

Переходы являются событиями reducer, а не набором независимых boolean flags. Это предотвращает состояния вида isListening=true и isSpeaking=true без осознанной поддержки duplex/barge-in.

### 11.5 Audio capture

- AudioRecord;
- PCM16 mono;
- запрашиваемая частота 16 kHz, с проверкой реальной поддержки устройства;
- audio source VOICE_RECOGNITION либо протестированная альтернатива;
- фиксированный frame clock;
- bounded channel для chunks;
- локальный VAD;
- 200–500 мс pre-roll ring buffer для wake phrase;
- AcousticEchoCanceler/NoiseSuppressor используются только если доступны и проходят device test;
- разрешение RECORD_AUDIO объясняется непосредственно перед системным prompt.

### 11.6 Audio playback

- AudioTrack streaming mode;
- PCM16 mono 24 kHz для Gemini Live;
- небольшой jitter buffer;
- audio attributes USAGE_ASSISTANT и CONTENT_TYPE_SPEECH;
- корректный audio focus;
- немедленные pause/flush/release при barge-in;
- discard всех chunks с отменённым response_id;
- метрики underrun count и buffer depth.

### 11.7 Локальные данные

Room хранит:

- список и entries недавно открытых conversations;
- optimistic pending message;
- cursor sync metadata;
- несекретное состояние последней открытой беседы.

PostgreSQL остаётся source of truth. После reconnect Android:

1. отправляет pending operation с тем же idempotency key;
2. загружает entries после последнего server sequence;
3. сливает их по server ID;
4. не пытается объединять сообщения только по тексту/времени.

### 11.8 Экраны PoC

- Splash/session restore;
- Sign in;
- Conversation list;
- Chat;
- Voice overlay/sheet;
- Settings: язык, голос, wake phrase toggle позже, sign out, delete account placeholder;
- Diagnostics в debug build: connection state, latency stages, audio format, provider alias.

Chat screen показывает:

- text и voice transcript единообразно;
- streaming assistant response;
- tool progress;
- interrupted/failed states;
- retry;
- кнопку voice mode.

### 11.9 Системный ассистент

Добавляется после стабильного in-app voice:

- VoiceInteractionService — очень лёгкий entrypoint в отдельном process;
- VoiceInteractionSessionService/Session — UI и запуск voice interaction;
- RecognitionService — если нужен полный VIA contract;
- запрос ROLE_ASSISTANT только после объясняющего onboarding;
- запуск из assist gesture сначала;
- hotword отдельным экспериментом.

Нельзя считать ROLE_ASSISTANT гарантией custom always-on hotword. AlwaysOnHotwordDetector зависит от SoundTrigger HAL, DSP, keyphrase enrollment и OEM. Если hardware path недоступен, CPU KWS с постоянным микрофоном должен отдельно оцениваться по ограничениям Android, индикации микрофона, батарее и правилам Google Play.

Capability matrix тестируется минимум на:

- AOSP/Pixel;
- Samsung;
- ещё одном распространённом OEM;
- экран включён/выключен;
- locked/unlocked;
- battery saver;
- после reboot;
- приложение выгружено пользователем;
- сеть потеряна/возвращена.

## 12. Wake word

Интерфейс:

~~~text
WakeWordEngine
  start(audioSource)
  detections() -> Flow<WakeWordDetection>
  stop()

WakeWordDetection
  phrase
  confidence
  detectedAt
  preRoll
~~~

Порядок:

1. Tap-to-talk — baseline качества голосового pipeline.
2. Foreground CPU KWS — проверка phrase/dataset/false activations.
3. Default assistant + assist gesture.
4. Background KWS capability spike.
5. Только после измерений — собственная KWS-модель или hardware/DSP integration.

Метрики:

- false accepts per hour;
- false rejects на наборе разных голосов;
- detection latency;
- CPU и battery drain за 8 часов;
- температура;
- устойчивость к музыке/телевизору;
- качество при выключенном экране.

Нельзя использовать Live API как постоянно включённый wake-word detector: это нарушает принцип локальной активации, увеличивает стоимость и отправляет фоновое аудио в облако.

## 13. Безопасность и приватность

### 13.1 Секреты

- .env не читается Android build;
- реальные .env и keystores не коммитятся;
- .env.example содержит только имена и безопасные placeholders;
- GEMINI_API_KEY и GOOGLE_CLIENT_SECRET доступны только backend process;
- Android содержит GOOGLE_CLIENT_ID, base URL и публичные параметры;
- логи config выводят только факт наличия секрета, никогда значение.

### 13.2 Authentication

- verify Google signature, aud, iss, exp и nonce;
- access token 10–15 минут;
- refresh token rotation и reuse detection;
- logout отзывает конкретную device session;
- delete account проектируется сразу, даже если UI появляется позже;
- rate limits по IP/user/device и отдельные limits для Live sessions.

### 13.3 Tool safety

- allow-list registry;
- schema validation;
- timeout и output size limit;
- SSRF protection для будущих HTTP connectors;
- запрет произвольных URL от модели;
- confirmation token связан с user, tool, args hash и сроком;
- write operation исполняется только после explicit confirmation;
- tool result считается недоверенным текстом и не может менять system policy.

### 13.4 Данные

- TLS для REST/WSS;
- raw voice не сохраняется по умолчанию;
- transcript сохраняется как часть истории с понятной настройкой удаления;
- payload логирование выключено по умолчанию;
- PII redaction в traces/errors;
- retention policy задаётся до внешней beta;
- database backups шифруются;
- пользователю позже доступны export/delete history.

## 14. Наблюдаемость и лимиты стоимости

На каждый turn создаётся correlation chain:

~~~text
request_id → conversation_id → turn_id → response_id → tool_execution_id
~~~

Метрики:

- HTTP/WS active sessions и error rate;
- provider connect latency;
- speech start/end timestamps;
- EOU → provider first event;
- EOU → first audio received backend;
- EOU → first audio played Android;
- text TTFT;
- tool duration/failures;
- audio queue depth/underruns;
- tokens/audio duration/cost per user and model alias;
- reconnect/interruption rate.

Логи — структурированные JSON. Distributed tracing можно добавить через OpenTelemetry. Crash reporting Android и backend error reporting не должны содержать сообщения или аудио.

Лимиты PoC:

- не более одной активной Live session на пользователя;
- максимальная длительность одной interaction session;
- idle timeout;
- дневной token/audio budget;
- максимальное число tool calls на turn;
- circuit breaker при provider errors;
- глобальный kill switch voice/text provider.

## 15. Локальная разработка и deployment

### 15.1 Local

Docker Compose:

~~~text
backend
postgres
redis
~~~

Gemini остаётся внешним API. Для обычных тестов AI_PROVIDER=fake; реальные smoke tests включаются явно.

Android:

- emulator обращается к host через 10.0.2.2;
- физическое устройство — через adb reverse или TLS tunnel;
- cleartext разрешается только debug network config;
- production-like WSS проверяется до voice milestone.

### 15.2 Staging PoC

Достаточно:

- один backend container;
- PostgreSQL;
- Redis;
- TLS reverse proxy/load balancer;
- secrets из deployment secret store;
- автоматические Alembic migrations отдельным release step;
- ежедневные backup и restore test;
- health/readiness probes.

Масштабирование backend по горизонтали возможно после вынесения realtime ephemeral state в Redis. Один live session всегда закрепляется за одним worker до reconnect; sticky routing либо session ownership registry выбирается при появлении второго instance.

### 15.3 Production evolution

Только по измеренной необходимости:

1. background worker для summaries/reminders;
2. object storage для artifacts;
3. отдельный realtime gateway;
4. отдельные tool workers;
5. локальные STT/LLM/TTS provider adapters;
6. GPU scheduler/pools;
7. event bus.

Kafka и Kubernetes не являются prerequisite ни для PoC, ни для первых пользователей.

## 16. Тестовая стратегия

### Backend unit

- auth token/nonce validation;
- conversation ownership;
- context budget;
- state transitions;
- idempotency;
- tool schemas/policy/confirmation;
- mapping Gemini events → AIEvent;
- error mapping.

### Backend integration

- PostgreSQL repositories и migrations;
- Redis nonce/session behavior;
- REST auth/history;
- SSE ordering/cancellation;
- WebSocket handshake, binary audio, backpressure;
- disconnect и interrupted turn;
- fake provider tool call.

### Contract

- OpenAPI snapshot;
- realtime JSON Schema;
- Android fixtures для всех server events;
- backward compatibility текущей версии протокола.

### Android unit

- auth/session repository;
- chat pagination/merge;
- voice reducer;
- cancellation и stale response filtering;
- retry/idempotency;
- PCM buffer calculations.

### Android instrumentation/device

- Credential Manager;
- microphone permission;
- AudioRecord/AudioTrack full duplex;
- screen rotation/process recreation;
- background/foreground;
- assist gesture;
- network handover Wi-Fi ↔ mobile;
- Bluetooth headset отдельно от phone speaker;
- lock screen.

### AI evaluation

Небольшой versioned corpus:

- русский;
- белорусский;
- смешанный RU/EN и BY/RU;
- имена, даты, адреса;
- короткие и длинные паузы;
- шум;
- перебивание;
- tool selection и аргументы;
- отказ от неразрешённых tools.

CI не зависит от качества внешней модели. Gemini eval/smoke запускается отдельно и сохраняет latency/quality report.

## 17. План реализации

### Этап 0. Решения и bootstrap

Backend:

- инициализировать git и Python project;
- добавить AGENTS/README с командами;
- .gitignore и безопасный .env.example;
- FastAPI skeleton, config validation;
- Docker Compose PostgreSQL/Redis;
- health endpoints;
- CI lint/typecheck/test;
- ADR для provider abstraction и realtime relay.

Android:

- создать соседний repository;
- Compose skeleton и module boundaries;
- debug/staging/prod build config;
- CI unit/lint/assemble;
- baseline navigation/design system.

Definition of Done:

- оба проекта собираются одной документированной командой;
- backend readiness проверяет DB/Redis;
- fake provider доступен без внешних credentials;
- ни один секрет не отслеживается git.

### Этап 1. Authentication

Backend:

- auth nonce;
- Google ID token verification;
- user upsert по sub;
- access/refresh token issuance;
- rotation/revocation;
- /me;
- auth audit и rate limit.

Android:

- Credential Manager;
- authorized-account flow и fallback all accounts;
- nonce;
- backend token exchange;
- secure session storage;
- restore/refresh/logout.

Definition of Done:

- clean install → Google sign-in → /me;
- истёкший access token обновляется прозрачно;
- logout отзывает refresh token;
- неверные aud/nonce/expired tokens отклоняются тестами.

### Этап 2. Conversation и text chat

Backend:

- migrations users/conversations/turns/entries;
- CRUD и cursor pagination;
- TextAIProvider + Fake + Gemini;
- SSE stream;
- context builder;
- idempotency;
- usage events.

Android:

- conversation list;
- chat timeline;
- Room cache;
- optimistic send;
- streaming delta rendering;
- retry/error state.

Definition of Done:

- текстовый разговор переживает restart приложения;
- duplicate retry не создаёт второе сообщение;
- fake E2E стабилен;
- Gemini text smoke проходит.

### Этап 3. Voice vertical slice по нажатию

Backend:

- realtime session endpoint;
- versioned WS protocol;
- GeminiLiveAdapter;
- binary PCM relay;
- transcript persistence;
- bounded queues/timeouts;
- realtime metrics.

Android:

- AudioRecord 16 kHz;
- AudioTrack 24 kHz;
- VoiceSessionController/state machine;
- voice overlay;
- transcript deltas;
- audio focus и permissions.

Definition of Done:

- запрос по нажатию получает потоковый голосовой ответ;
- text и voice видны в одной беседе;
- первый звук измеряется end-to-end;
- сырое аудио не попадает в БД/логи;
- disconnect даёт понятный recoverable state.

### Этап 4. Duplex, VAD и barge-in

Backend:

- interruption/cancel events;
- stale response filtering;
- local end-of-speech commit;
- provider resumption/reconnect;
- latency stage telemetry.

Android:

- локальный VAD;
- simultaneous capture/playback;
- immediate AudioTrack flush;
- jitter buffer;
- pre-roll buffer;
- device audio diagnostics.

Definition of Done:

- пользователь перебивает ассистента без наложения старого аудио;
- p50/p95 latency видны в dashboard/report;
- 20 последовательных turns не приводят к утечке ресурсов;
- сетевой handover не ломает завершённую историю.

### Этап 5. Первый tool

Backend:

- registry/executor/policy/audit;
- function declarations Gemini;
- read-only weather/time tool;
- tool progress events;
- timeouts и output sanitization.

Android:

- inline tool progress/result;
- retry UX;
- confirmation component проектируется, но write-tool можно не включать.

Definition of Done:

- модель выбирает tool, backend валидирует args, исполняет и возвращает результат;
- вызов виден в аудите и history;
- неизвестный/невалидный tool отклоняется;
- prompt injection в tool output не меняет policy.

### Этап 6. Wake phrase и системный assistant spike

Android:

- WakeWordEngine interface;
- foreground KWS implementation;
- false accept/reject test corpus;
- ROLE_ASSISTANT onboarding;
- VoiceInteractionService/Session;
- assist gesture;
- проверка AlwaysOnHotwordDetector availability;
- OEM capability matrix и battery measurements.

Definition of Done:

- foreground wake phrase запускает тот же voice flow;
- системный assist gesture открывает voice session;
- документированы реально работающие режимы на тестовых устройствах;
- решение о background custom hotword принято по данным, а не предположению.

### Этап 7. Internal beta hardening

- account/history deletion;
- privacy copy и microphone disclosure;
- quotas/cost limits;
- crash/metrics dashboards;
- provider circuit breaker;
- backup/restore drill;
- Play AI-generated-content policy checklist;
- accessibility;
- плохая сеть, Bluetooth и lock-screen matrix;
- release signing и staging/prod separation.

Definition of Done:

- beta build пригоден для ежедневного использования небольшой группой;
- стоимость, latency и failure rate наблюдаемы;
- rollback backend и Android release задокументирован;
- критические действия отсутствуют либо требуют подтверждения.

## 18. Очерёдность первых задач

Первые задачи должны идти в таком порядке:

1. Backend skeleton + Compose skeleton.
2. FakeAIProvider и единый event model.
3. DB schema conversations/turns/entries.
4. Google auth end-to-end.
5. Text chat end-to-end на fake.
6. Gemini text adapter.
7. Realtime protocol contract и fake realtime server.
8. Android AudioRecord/AudioTrack loopback test.
9. Gemini Live relay.
10. Transcript persistence.
11. Local VAD и barge-in.
12. Tool registry и один read-only tool.
13. Wake phrase foreground.
14. ROLE_ASSISTANT spike.

Такой порядок изолирует риски. Когда начинается Gemini Live integration, уже существуют authentication, canonical history, fake provider, Android audio proof и проверяемый protocol.

## 19. Архитектурные проверки перед началом реализации

Нужно подтвердить без изменения общего проекта:

- GOOGLE_CLIENT_ID относится к Web OAuth client и подходит как server client ID;
- для Android создан OAuth client с правильными package name и SHA-1 signing certificate;
- получен отдельный Gemini credential;
- выбран package name Android и application ID;
- выбрана wake phrase;
- выбраны минимальная и target Android API;
- выбран первый tool и его источник данных;
- решено, хранить ли output transcript Gemini как точный UI-текст или как приблизительный transcript;
- определён лимит длительности и дневной стоимости Live session.

Если ответы пока неизвестны, используются безопасные defaults:

- package name временный, но уникальный;
- tap-to-talk до выбора wake phrase;
- system.get_current_datetime как первый tool;
- output transcript помечается как transcript, а не как гарантированно точный текст произнесённого аудио;
- session и cost limits задаются консервативно через config.

## 20. Риски и способы снижения

| Риск | Последствие | Снижение |
| --- | --- | --- |
| Gemini Live preview меняет API/model ID | поломка voice | adapter, config model alias, smoke tests |
| Задержка внешнего API | плохой voice UX | streaming, hybrid VAD, bounded buffers, stage metrics |
| Transcript расходится с аудио | странная история | хранить delivery metadata, явно считать его transcript |
| Tool hallucination/invalid args | ошибка или опасное действие | registry, schema, policy, confirmation, idempotency |
| WebSocket reconnect теряет состояние | оборванный диалог | PostgreSQL canonical state, interrupted status, idempotency |
| OEM не даёт custom DSP hotword | нет Hey-MIA при screen-off | capability spike, assist gesture, CPU KWS evaluation |
| Full-duplex даёт echo | ложный barge-in | AEC where available, audio focus, device tests |
| Рост стоимости audio sessions | непредсказуемый счёт | duration/quota limits, usage records, kill switch |
| Секрет попадает в APK/лог | компрометация | backend-only credentials, scanning, log redaction |
| Слишком ранние микросервисы | замедление разработки | модульный монолит до измеренной нагрузки |

## 21. Зафиксированные ADR

### ADR-001: модульный монолит

PoC реализуется одним backend deployment. Модули имеют внутренние границы и могут быть вынесены позже, но распределённость не создаётся заранее.

### ADR-002: backend-mediated Gemini Live

Android соединяется с MIA backend, а не напрямую с Gemini. Это упрощает tool policy, аудит, историю и provider swap. Direct client-to-Gemini с ephemeral token можно сравнить по latency позже.

### ADR-003: provider-neutral events

Вся orchestration работает с внутренними AIEvent. Типы и model IDs Google не пересекают границу providers/.

### ADR-004: единая история

Voice и text являются разными modalities одного Conversation/Turn/Entry aggregate, а не отдельными хранилищами.

### ADR-005: local wake word

Аудио до wake phrase не передаётся в облако. Первым trigger остаётся tap-to-talk, затем foreground KWS и только затем background assistant integration.

### ADR-006: явный tool loop

Function calling всегда проходит через backend ToolRegistry, ToolPolicy и ToolExecutor. SDK automatic execution не используется.

### ADR-007: raw audio ephemeral

Сырое аудио живёт только в bounded memory buffers активной session и не сохраняется по умолчанию.

## 22. Официальные технические источники

- Gemini Live WebSocket: https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket
- Gemini Live capabilities и audio/VAD: https://ai.google.dev/gemini-api/docs/live-api/capabilities
- Gemini Live best practices: https://ai.google.dev/gemini-api/docs/live-api/best-practices
- Gemini Live session management: https://ai.google.dev/gemini-api/docs/live-api/session-management
- Gemini Live tool use: https://ai.google.dev/gemini-api/docs/live-api/tools
- Gemini function calling: https://ai.google.dev/gemini-api/docs/function-calling
- Android Sign in with Google через Credential Manager: https://developer.android.com/identity/sign-in/credential-manager-siwg-implementation
- Регистрация Android OAuth client и SHA-1 сертификата: https://developers.google.com/android/guides/client-auth
- Проверка Google ID token на backend: https://developers.google.com/identity/gsi/web/guides/verify-google-id-token
- VoiceInteractionService: https://developer.android.com/reference/android/service/voice/VoiceInteractionService
- Android background microphone restrictions: https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start
- AOSP hotword integration и ограничения SoundTrigger/DSP: https://source.android.com/docs/automotive/voice/voice_interaction_guide/integration_flows

## 23. Итог

PoC не должен имитировать всю будущую колонку. Он должен доказать один сквозной контур:

~~~text
Google user
→ unified conversation
→ text or live voice
→ Gemini behind provider interface
→ controlled tool
→ streamed answer
→ persistent cross-modal history
~~~

После этого wake word является новым локальным trigger, Raspberry Pi — новым клиентом realtime protocol, а локальные STT/LLM/TTS — новой реализацией provider interfaces. Ни один из этих шагов не требует менять основу аккаунтов, разговоров, tool policy или Android UI.
