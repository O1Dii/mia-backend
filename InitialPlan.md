Да. Здесь я бы строил не просто «умную колонку», а **локальную платформу голосового AI-ассистента**, у которой колонка — лишь один из клиентов. Тогда пользователь начинает разговор голосом на телефоне или колонке, продолжает его текстом в приложении/Telegram, получает туда длинные ответы, файлы и результаты исследований, а разработчики расширяют систему через единый SDK плагинов.

### Как должна выглядеть система

Архитектурно я бы разделил её так:

```text
                         ┌─────────────────────────┐
                         │      USER ACCOUNT       │
                         │ history / memory / auth │
                         └────────────┬────────────┘
                                      │
                 ┌────────────────────┼────────────────────┐
                 │                    │                    │
          Android App            Smart Speaker       Telegram / Web
        wake word + chat       wake word + audio        full chat
                 │                    │                    │
                 └────────────────────┼────────────────────┘
                                      │
                             Realtime Voice Gateway
                         WebSocket / WebRTC / gRPC stream
                                      │
                   ┌──────────────────┼───────────────────┐
                   │                  │                   │
                  STT              ORCHESTRATOR          TTS
             speech → text       intents / tools      text → voice
                                      │
                         ┌────────────┴────────────┐
                         │                         │
                       LLM                  Plugin Platform
                                                 │
                         ┌───────────┬────────────┼────────────┐
                         │           │            │            │
                       Home       Calendar      Search       Custom
                     Assistant      etc.        etc.         plugins
```

Самое важное: **wake word и первичная обработка звука должны выполняться локально**, чтобы постоянный поток с микрофона не уходил на сервер. На сервер аудио начинает передаваться только после активационной фразы.

Большая LLM при этом работает централизованно в Беларуси.

---

# Что я предлагаю делать по этапам

1. **Сначала вообще не делать колонку.** Сделать Android-приложение, которое уже ведёт себя как будущая колонка: wake word → запись фразы → STT → LLM → streaming TTS → голосовой ответ. Одновременно приложение содержит обычный AI-чат. На этом этапе сервер можно держать даже на своей машине с NVIDIA GPU или на одном арендованном GPU-сервере.

2. **Сделать единый backend AI Platform.** Отделить STT, LLM, TTS, аккаунты, conversation service и tool/plugin execution друг от друга. Клиенту должно быть безразлично, откуда пришёл запрос: Android, Telegram, браузер или колонка.

3. **Добавить настоящего системного Android-ассистента.** Приложение просит пользователя назначить его Digital Assistant. Android имеет `ROLE_ASSISTANT`, а выбранный `VoiceInteractionService` система поддерживает работающим для функций вроде hotword. Это значительно правильнее, чем постоянно держать обычный foreground-service с микрофоном; Android всё сильнее ограничивает доступ фоновых приложений к микрофону. ([Android Developers][1])

4. **Сделать Plugin SDK до появления физической колонки.** Погода, таймеры, умный дом, календарь, музыка и сторонние сервисы должны быть инструментами платформы, а не `if/else` внутри голосового приложения.

5. **Перенести voice-client с Android на Raspberry Pi 5.** Сначала буквально поставить Pi на стол, USB microphone array и обычную колонку. Voice Client должен использовать практически тот же протокол, что телефон.

6. **Сделать Speaker Prototype v1.** Raspberry Pi 5 + микрофонный массив + USB/I²S audio + динамики + усилитель + LED-ring + кнопка mute. Здесь решается значительно более сложная часть продукта: echo cancellation, beamforming, распознавание голоса с 3–5 метров, разговор при играющей музыке.

7. **После проверки UX перейти с Raspberry Pi 5 на Compute Module 5 и свою carrier board.** CM5 именно для таких embedded-продуктов: небольшой модуль 55×40 мм, Cortex-A76, варианты 2–16 GB RAM, eMMC, Wi-Fi/Bluetooth, USB/I²S/GPIO и другие интерфейсы. ([Raspberry Pi][2])

8. **Только потом проектировать финальный корпус и готовить производство.** Перед этим уже должны быть измерены температура, питание, акустика, микрофоны, Wi-Fi, OTA-обновления и реальные false-positive/false-negative wake-word показатели.

---

# Что именно делать первым

Я бы сделал первый proof-of-concept **только под Android**.

Не React Native и не Flutter для voice-core. UI можно потом вынести куда угодно, но системный ассистент, audio pipeline и background voice лучше написать на Kotlin.

Минимальный сценарий:

```text
Телефон лежит с выключенным экраном

        ↓

«Привет, <имя ассистента>»

        ↓
local wake-word

        ↓
короткий звуковой feedback

        ↓
«Что сегодня с погодой в Минске?»

        ↓
streaming audio → сервер

        ↓
streaming STT

        ↓
LLM понимает:
tool = weather

        ↓
plugin weather

        ↓
LLM формирует ответ

        ↓
streaming TTS

        ↓
телефон начинает говорить,
не дожидаясь генерации всего ответа
```

Целевой UX — чтобы после конца пользовательской фразы **звук ответа начинался примерно через 0,7–1,5 секунды**, а не через 5–10 секунд.

Это критично. Для голосового ассистента модель, которая чуть хуже, но начинает отвечать за секунду, обычно воспринимается гораздо лучше модели, которая думает восемь секунд.

---

# «Так же удобно, как Окей Google» на телефоне

На **Android это наиболее реалистичная часть идеи**.

Android позволяет приложению получить роль ассистента `ROLE_ASSISTANT`; пользователь должен явно выбрать ваше приложение. `VoiceInteractionService` как раз является специальным системным механизмом для постоянно доступного голосового взаимодействия и hotword. ([Android Developers][3])

Это даёт возможность сделать:

**«Привет, Лея» → ваш ассистент открывается поверх текущего приложения → слушает → отвечает → закрывается.**

Также можно поддержать вызов удержанием кнопки питания/жестом ассистента на устройствах, где Android/OEM позволяет выбрать стороннего помощника.

Это намного перспективнее попытки просто запустить обычное приложение с постоянно включённым микрофоном: Android 14+ существенно ограничивает microphone foreground services в фоне. Для `VoiceInteractionService` есть специальное исключение. ([Android Developers][4])

### А вот с iPhone ситуация хуже

Можно интегрироваться через Siri/App Intents, включая intents, способные работать даже на заблокированном устройстве, но сделать стороннему приложению собственный полноценный глобальный аналог фразы **«Hey Siri»** значительно проблематичнее. Apple предоставляет приложениям Siri/App Intents, а не замену системной Siri. ([Apple Developer][5])

Поэтому я бы сознательно делал:

**Android — основной voice-first клиент.**

**iOS — приложение + chat + push + Siri shortcuts/App Intents.**

---

# Wake word — отдельная важная проблема

Для английского есть много готовых решений. С русским/белорусским сложнее.

Например, популярный openWakeWord сейчас официально заявляет поддержку только английских моделей. ([GitHub][6])

Porcupine очень хорош технически, работает на Android и Raspberry Pi и поддерживает custom wake words, но его официальный перечень языков пока не включает русский или белорусский. ([Picovoice][7])

Поэтому для коммерческой системы я бы заложил собственный маленький **KWS — Keyword Spotting model**.

Это не LLM. Это очень небольшая neural network, задача которой:

```text
audio →
«сказана ли фраза Привет Лея?» →
YES / NO
```

Она может весить единицы мегабайт и работать постоянно даже на слабом ARM CPU.

На первом прототипе можно вообще использовать локальный streaming ASR/VAD и имитировать wake-word, а параллельно собрать датасет из нескольких сотен/тысяч произношений выбранной фразы.

Интересный фундамент для экспериментов — sherpa-onnx: там есть ONNX deployment для Android и механизм open-vocabulary keyword spotting, а само семейство инструментов предназначено для локального STT/VAD/TTS. Сейчас готовые KWS-модели у них в основном EN/ZH, поэтому русский KWS всё равно придётся отдельно подготовить. ([K2 FSA][8])

---

# Какой AI stack я бы выбрал

### LLM

Очень интересный стартовый вариант сейчас — **Qwen3**.

Причина не только в качестве. Семейство официально заявляет 119 языков и диалектов, включая **русский и белорусский**, а также рассчитано на agent/tool calling. ([Qwen][9])

Для разработки:

```text
Qwen3 8B
```

Для более серьёзной версии:

```text
Qwen3-30B-A3B-Instruct
```

У последней 30,5 млрд параметров всего, но около 3,3 млрд активны на токен; модель рассчитана в том числе на tool use и имеет Apache-2.0 license. ([Hugging Face][10])

То есть я бы не пытался сразу запустить 70B-монстра.

### Inference

На ноутбуке / локальной разработке:

```text
llama.cpp
```

Он умеет quantized-модели, OpenAI-compatible API, concurrent users, structured JSON и function calling. ([GitHub][11])

На GPU production-серверах:

```text
vLLM
```

Он уже даёт OpenAI-compatible Chat/Responses API, structured output и tool calling. ([vLLM][12])

Очень полезно сделать свой API совместимым с OpenAI schema:

```text
POST /v1/responses
POST /v1/chat/completions
```

Тогда модель можно менять практически без изменения платформы.

---

# Speech-to-text

Я бы поддерживал минимум два движка.

Server STT:

```text
Whisper-family
        ↓
GPU
        ↓
основное качественное распознавание
```

Local fallback:

```text
sherpa-onnx / небольшой ASR
        ↓
Android / Speaker
```

Sherpa-onnx прямо поддерживает полностью локальный realtime ASR на Android. ([K2 FSA][13])

Очень желательно поддержать **русский + белорусский + English code switching**:

> «Лея, напомни мне tomorrow at seven позвонить Алексею».

Такие смешанные команды пользователь будет произносить постоянно.

---

# TTS

Для прототипа я бы начал с **Silero TTS**.

У него хорошие локальные русские модели, несколько голосов, автоматическое ударение/омографы и CPU/GPU inference. ([GitHub][14])

Альтернативный вариант — Piper: у него тоже есть русские голоса и ARM64/Raspberry Pi support. ([GitHub][15])

Но в конечном продукте голос станет частью бренда, поэтому со временем я бы обучил собственный TTS voice:

```text
«Лея» должна всегда звучать как «Лея».
```

---

# Очень важная вещь: streaming

Нельзя делать:

```text
record.wav
↓
STT целиком
↓
LLM целиком
↓
TTS целиком
↓
проиграть
```

Нужно:

```text
MIC
 ↓
audio chunks
 ↓
Streaming STT
 ↓
partial transcript
 ↓
LLM streaming
 ↓
sentence chunking
 ↓
Streaming TTS
 ↓
speaker
```

Например LLM уже сказала:

> «Сегодня в Минске...»

TTS сразу начинает синтезировать эту часть, пока LLM ещё генерирует остаток ответа.

И обязательна **barge-in**:

Ассистент говорит:

> «Сегодня ожидается температура…»

Пользователь:

> «Стоп».

Звук мгновенно прекращается.

Без barge-in система не ощущается живым ассистентом.

---

# Колонка должна быть относительно «тупой»

На ней я бы оставил:

| Компонент               | Где                 |
| ----------------------- | ------------------- |
| Wake word               | колонка             |
| VAD                     | колонка             |
| Noise suppression       | колонка             |
| AEC / echo cancellation | колонка             |
| Beamforming             | колонка             |
| кнопка mute             | физическая          |
| LED feedback            | колонка             |
| базовые таймеры         | желательно локально |
| STT                     | сервер              |
| LLM                     | сервер              |
| TTS                     | в основном сервер   |
| plugins                 | сервер              |
| conversation history    | сервер              |
| memory                  | сервер              |

И небольшой offline mode:

```text
«Стоп»
«Громче»
«Тише»
«Пауза»
«Продолжить»
«Поставь таймер на 5 минут»
```

должны работать даже при падении интернета.

---

# Железо первой колонки

Прототип:

```text
Raspberry Pi 5 4/8 GB
+
ReSpeaker / другой 4–6 microphone array
+
I²S / USB DAC
+
Class-D amplifier
+
2 динамика
+
LED ring
+
physical mute switch
+
USB-C PSU
```

ReSpeaker как раз предлагает microphone arrays для проектов наподобие DIY Echo/Google Home, включая варианты с аппаратной front-end обработкой звука и AEC. ([Seeed Studio Wiki][16])

После proof-of-concept:

```text
Compute Module 5
+
custom PCB
+
mic array
+
audio DSP
+
amp
+
Wi-Fi/Bluetooth
+
LED controller
```

Причём я бы серьёзно рассмотрел отдельный audio DSP/XMOS для production, а не заставлял CPU Pi заниматься всей far-field обработкой.

---

# Платформа плагинов

Вот здесь может находиться одно из главных преимуществ вашего продукта.

Я бы не заставлял разработчика писать специальный код для «колонки».

Он описывает **Tool**.

Например:

```json
{
  "name": "todoist.add_task",
  "description": "Add a task to Todoist",
  "parameters": {
    "title": "string",
    "due": "string"
  }
}
```

LLM получает набор разрешённых tools:

```text
calendar.create_event
calendar.list_events

home.light.turn_on
home.temperature.get

music.search
music.play

taxi.get_price

weather.current

todoist.add_task
```

И сам решает, какой вызвать.

---

# Manifest плагина

Например:

```yaml
id: by.example.weather
name: Belarus Weather
version: 1.2.0

permissions:
  - location.approximate
  - network

tools:
  - get_weather
  - get_forecast

entrypoint:
  https://api.example.by/assistant
```

Или developer может поставлять container/WASM plugin.

---

# Я бы поддержал три типа расширений

Это уже важный продуктовый момент.

### Cloud connector

Разработчик держит API у себя:

```text
ваша платформа → HTTPS → API разработчика
```

Самый простой вариант.

### Hosted plugin

Разработчик загружает код:

```text
plugin
↓
isolated container/WASM sandbox
↓
ваша инфраструктура
```

Это удобнее обычному разработчику.

### MCP

Добавить MCP gateway тоже очень разумно.

Qwen3 уже ориентирован на agentic/tool сценарии, включая MCP-направление. ([Qwen][9])

Тогда разработчики смогут адаптировать существующие MCP servers вместо написания интеграции специально для вашей платформы.

---

# Но plugins нельзя запускать без permission model

Например:

```text
Plugin asks:

✓ Read calendar
✓ Create calendar events
✗ Delete calendar events
✗ Access microphone
✗ Access conversation history
```

LLM не должен иметь возможность сам себе дать дополнительное разрешение.

Особенно действия:

```text
купить
оплатить
удалить
отправить
открыть дверь
вызвать такси
```

должны иметь подтверждение.

Например:

> — Заказать такси за 18 рублей?
> — Да.

Только после этого tool invocation.

---

# Что должно быть в приложении

Я бы не делал его «приложением для настройки колонки». Оно должно быть полноценным интерфейсом ассистента.

| Обязательно          | Позже                    |
| -------------------- | ------------------------ |
| AI chat              | голосовые комнаты        |
| история разговоров   | семейные аккаунты        |
| voice conversation   | speaker recognition      |
| управление колонками | дети / parental profile  |
| плагины              | marketplace              |
| permissions          | automation builder       |
| настройки памяти     | камеры                   |
| выбор голоса         | мультимодальность        |
| push                 | локальный smart-home hub |
| длинные ответы       | персональные agents      |
| результаты research  | developer mode           |

---

# Очень важная функция: Voice → продолжить в телефоне

Например:

> Пользователь: «Лея, сравни мне четыре лучших робота-пылесоса до 1200 рублей».

Колонка:

> «Хорошо. Кратко расскажу главное, а полное сравнение с таблицей отправлю в приложение».

После этого в chat появляется:

```text
┌────────────────────────────────┐
│ Исследование                   │
│ Роботы-пылесосы до 1200 BYN    │
│                                │
│ ✓ comparison table             │
│ ✓ pros/cons                    │
│ ✓ sources                      │
│ ✓ recommendation               │
└────────────────────────────────┘
```

И разговор остаётся единым.

Это очень важная идея: **voice — это не отдельный продукт от chat**.

---

# Telegram

Telegram хорош как дополнительный frontend.

Bot API + Mini Apps позволяют построить достаточно богатый интерфейс внутри Telegram. ([Telegram Core][17])

Например:

```text
Telegram Bot

/text
/voice
/images
/files

+
Mini App
↓
history
plugins
devices
subscription
```

Но Telegram я бы не делал основным клиентом платформы.

Есть ещё юридический аспект: в Беларуси действует Закон №99-З «О защите персональных данных», включая правила трансграничной передачи данных. ([Спинформ][18])

Поэтому архитектурно лучше:

```text
Canonical user data
        ↓
ваши серверы BY

Telegram
        ↓
transport / UI only
```

И отдельно проработать согласия/политику обработки данных. Для коммерческого запуска здесь уже нужен белорусский юрист по ПДн.

---

# Где размещать AI в Беларуси

Для MVP не обязательно покупать свой GPU.

На белорусском рынке уже существуют варианты GPU infrastructure. Например ITGLOBAL рекламирует GPU Cloud с ЦОД в Минске и A100/A800/RTX PRO 6000 конфигурациями, а cloud.by имеет конфигуратор выделенных GPU servers. Перед договором я бы отдельно проверял **фактическую физическую локацию конкретного GPU, SLA и режим хранения данных**, а не полагался только на маркетинговое обозначение услуги. ([ItGlobal][19])

На старте:

```text
1 × GPU
↓
STT
LLM
TTS
```

Позже разделить:

```text
GPU pool #1 → STT
GPU pool #2 → LLM
GPU pool #3 → TTS
```

А inference ставить за scheduler:

```text
             ┌─ LLM worker
API Gateway ─┼─ LLM worker
             └─ LLM worker
```

Это позволит добавлять GPU по мере роста.

---

# Как должна выглядеть серверная архитектура

С учётом твоего Python/JS background я бы сделал примерно так:

```text
                    Nginx / Envoy
                         │
                   API Gateway
                         │
          ┌──────────────┼──────────────┐
          │              │              │
       REST API      Realtime API    Plugin API
       FastAPI       WebSocket        FastAPI
          │              │
          └──────┬───────┘
                 │
           AI Orchestrator
                 │
      ┌──────────┼───────────┐
      │          │           │
     STT        LLM         TTS
   workers     vLLM       workers
      │          │           │
      └──────────┼───────────┘
                 │
             Tool Router
                 │
        ┌────────┼─────────┐
        │        │         │
      Plugin    MCP    internal tools
        │
        │
     Sandbox

PostgreSQL
Redis
S3-compatible object storage
NATS/Kafka eventually
Vault/secrets manager
```

На первом MVP Kafka вообще не нужен.

Достаточно:

```text
FastAPI
PostgreSQL
Redis
MinIO
vLLM
Docker Compose
```

---

# Conversation architecture

И это лучше правильно сделать сразу.

Не хранить:

```text
telegram_messages
speaker_messages
android_messages
```

Отдельно.

Должно быть:

```text
User
  │
Conversation
  │
  ├── VoiceTurn
  ├── TextTurn
  ├── ToolCall
  ├── ToolResult
  └── Artifact
```

И:

```text
source:
 speaker
 android
 ios
 web
 telegram
```

Тогда пользователь говорит колонке:

> «Найди билеты Минск — Стамбул».

А через минуту открывает телефон и пишет:

> «А только без пересадки».

И модель понимает контекст.

---

# Memory

Я бы разделил память минимум на три уровня.

```text
Conversation context
↓
текущий диалог


User memory
↓
«Я предпочитаю места у прохода»


Private integrations
↓
calendar / tasks / smart home
```

И обязательно сделать пользователю экран:

```text
Что ассистент знает обо мне
```

с возможностью просматривать и удалять записи.

Это одновременно улучшит доверие к продукту и упростит privacy.

---

# Базовый функционал колонки

Первый коммерчески полезный набор я вижу таким:

| Категория   | Возможности                        |
| ----------- | ---------------------------------- |
| AI          | вопросы, диалог, объяснения        |
| Search      | свежая информация                  |
| Weather     | текущая/прогноз                    |
| Timers      | таймеры/будильники                 |
| Reminders   | напоминания                        |
| Calendar    | расписание                         |
| Lists       | покупки/заметки                    |
| Music       | playback                           |
| Radio       | интернет-радио                     |
| Smart Home  | Home Assistant/MQTT                |
| Messaging   | Telegram                           |
| Translation | перевод                            |
| News        | краткие дайджесты                  |
| Research    | длинное исследование → телефон     |
| Knowledge   | работа с пользовательскими файлами |
| Plugins     | сторонние integrations             |

---

# Функции, которые я считаю особенно интересными

Например:

> «Что у меня сегодня?»

Ассистент объединяет:

```text
calendar
+
reminders
+
weather
+
traffic
```

И выдаёт одну нормальную реплику.

Или:

> «Я ухожу».

Это не hard-coded command.

LLM запускает automation:

```text
lights.off
thermostat.away
music.stop
robot.start
```

---

# Затем automations

Разработчик или пользователь сможет создать:

```text
WHEN:
weekday = Monday–Friday
time = 07:30

IF:
user_home = true

DO:
tell:
  weather
  calendar
  traffic
  reminders
```

Или естественным языком:

> «Каждое утро после будильника рассказывай погоду и что у меня первого в календаре».

LLM превращает это в workflow.

Это потенциально значительно сильнее классических «навыков» Алисы.

---

# Speaker recognition

Позже очень полезно.

Колонка слышит:

> «Что у меня сегодня?»

И понимает, кто спросил.

```text
voice embedding
        ↓
Aliaksei
        ↓
his calendar
```

Другой член семьи:

```text
voice embedding
        ↓
Anna
        ↓
her calendar
```

Но это я бы оставил после MVP, потому что speaker identification влияет и на security, и на privacy.

---

# Физическая приватность

На колонке обязательно должен быть **аппаратный выключатель микрофонов**.

Не:

```text
button
↓
software
↓
microphone disabled
```

А:

```text
physical switch
↓
power/clock line microphone array disconnected
```

И LED должен аппаратно показывать состояние микрофонов.

Это сильный аргумент продукта:

> **Когда микрофон выключен — программно включить его невозможно.**

---

# Что продавать

Твоя идея с отдельной покупкой устройства и подпиской выглядит логично.

Я бы сделал примерно такую модель:

| Hardware                  | Subscription       |
| ------------------------- | ------------------ |
| покупается один раз       | ежемесячно         |
| колонка                   | AI inference       |
| 1 год гарантии            | STT/TTS            |
| базовые локальные функции | search             |
| offline commands          | memory             |
| Bluetooth speaker         | cloud integrations |
|                           | research           |
|                           | advanced plugins   |

При этом **колонка не должна превращаться в кирпич без подписки**.

Без подписки оставить:

```text
Bluetooth
local alarms
timers
local smart-home commands
basic offline assistant
```

Это будет сильно лучше восприниматься покупателями.

---

# А developers

Отдельный portal:

```text
developers.yourassistant.by
```

В нём:

```text
My plugins
API keys
OAuth
Webhook logs
Test console
Tool schemas
Usage
Versions
Permissions
Marketplace publishing
```

И CLI:

```bash
assistant login

assistant plugin init

assistant plugin dev

assistant plugin test

assistant plugin publish
```

Локальный dev server:

```text
Developer laptop
    ↑
secure tunnel
    ↑
Assistant Cloud
```

Он говорит телефону:

> «Лея, сколько денег на тестовом счёте?»

и тут же видит invocation своего local plugin.

---

# Что я считаю главным отличием от Алисы/Google Home

Не пытаться конкурировать количеством predefined commands.

Сделать продукт:

**«AI operating system for voice»**.

То есть:

```text
LLM
+
voice
+
persistent conversation
+
memory
+
tools
+
developer ecosystem
+
devices
```

И колонка является просто **домашним терминалом этой системы**.

Телефон — мобильным терминалом.

Telegram — альтернативным терминалом.

Web — рабочим терминалом.

---

# Какой MVP я бы реально строил

Если сильно сократить scope, первая версия должна содержать всего это:

```text
Android
  ├ wake phrase
  ├ voice overlay
  ├ chat
  └ conversation history

Server
  ├ authentication
  ├ streaming STT
  ├ Qwen
  ├ streaming TTS
  ├ conversations
  └ tool router

Tools
  ├ weather
  ├ web search
  ├ timer/reminders
  └ Home Assistant

Telegram
  └ same conversations

Hardware
  └ NONE
```

И только когда такой ассистент **реально хочется использовать каждый день на телефоне**, переносить client pipeline на Raspberry Pi.

Это позволит не потратить месяцы на корпус, PCB и акустику, а потом обнаружить, что сама conversational experience неудобна.

После этого путь уже довольно прямой:

```text
Android prototype
        ↓
Raspberry Pi prototype
        ↓
acoustic prototype
        ↓
CM5 engineering prototype
        ↓
custom PCB
        ↓
industrial design
        ↓
certification
        ↓
small batch
        ↓
mass production
```

Именно такой порядок реализации я считаю для этого проекта наиболее рациональным. Причём **первые несколько месяцев вообще можно не покупать ни одного компонента будущей колонки кроме Android-телефона и GPU-сервера** — сначала нужно доказать, что wake-word → разговор → tools → продолжение в чате действительно ощущается как продукт, которым хочется пользоваться.

[1]: https://developer.android.com/reference/android/app/role/RoleManager?utm_source=chatgpt.com "RoleManager  |  API reference  |  Android Developers"
[2]: https://www.raspberrypi.com/products/compute-module-5/?utm_source=chatgpt.com "Buy a Raspberry Pi Compute Module 5 – Raspberry Pi"
[3]: https://developer.android.com/reference/android/app/role/RoleManager?authuser=7&utm_source=chatgpt.com "RoleManager  |  API reference  |  Android Developers"
[4]: https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start?utm_source=chatgpt.com "Restrictions on starting a foreground service from the background  |  Background work  |  Android Developers"
[5]: https://developer.apple.com/documentation/xcode/configuring-siri-support?utm_source=chatgpt.com "Configuring Siri support | Apple Developer Documentation"
[6]: https://github.com/dscripka/openWakeWord?utm_source=chatgpt.com "GitHub - dscripka/openWakeWord: An open-source audio wake word (or phrase) detection framework with a focus on performance and simplicity. · GitHub"
[7]: https://picovoice.ai/docs/faq/porcupine/?utm_source=chatgpt.com "FAQ | Porcupine Wake Word Detection Engine - Picovoice Docs"
[8]: https://k2-fsa.github.io/sherpa/onnx/kws/index.html?utm_source=chatgpt.com "Keyword spotting — sherpa 1.3 documentation"
[9]: https://qwenlm.github.io/blog/qwen3/?utm_source=chatgpt.com "Qwen3: Think Deeper, Act Faster | Qwen"
[10]: https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507?utm_source=chatgpt.com "Qwen/Qwen3-30B-A3B-Instruct-2507 · Hugging Face"
[11]: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md?utm_source=chatgpt.com "llama.cpp/tools/server/README.md at master · ggml-org/llama.cpp · GitHub"
[12]: https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/?utm_source=chatgpt.com "OpenAI-Compatible Server - vLLM"
[13]: https://k2-fsa.github.io/sherpa/onnx/android/index.html?utm_source=chatgpt.com "Android — sherpa 1.3 documentation"
[14]: https://github.com/snakers4/silero-models?utm_source=chatgpt.com "GitHub - snakers4/silero-models: Silero Models: pre-trained text-to-speech models made embarrassingly simple · GitHub"
[15]: https://github.com/rhasspy/piper/blob/master/VOICES.md?utm_source=chatgpt.com "piper/VOICES.md at master · rhasspy/piper · GitHub"
[16]: https://wiki.seeedstudio.com/ReSpeaker_Product_Guide/?utm_source=chatgpt.com "reSpeaker Product Guide | Seeed Studio Wiki"
[17]: https://core.telegram.org/bots/webapps?utm_source=chatgpt.com "Telegram Mini Apps"
[18]: https://base2.spinform.ru/show_doc.fwx?rgn=132096&utm_source=chatgpt.com "Закон Республики Беларусь \"О защите персональных данных\""
[19]: https://itglobal.com/ru-by/services/virtual-infrastructure/arenda-oblachnyh-gpu-serverov/?utm_source=chatgpt.com "Аренда сервера GPU в Беларуси | Серверы GPU NVIDIA | ITG BY"
