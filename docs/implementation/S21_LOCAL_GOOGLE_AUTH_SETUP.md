# S21: локальная проверка Google sign-in на телефоне

Этот runbook закрывает внешний блокер [S21-R1](handoffs/S21-R1.md). Сейчас
Keystore-тест на телефоне прошёл, но OAuth-клиенты и адрес backend не настроены.
Для PoC временно используем **`by.mia.assistant.debug`** из Android
`app/build.gradle.kts`; это не решение о release ID (OI-ARCH-001). Backend и БД
работают локально, ngrok даёт телефону временный HTTPS-адрес. Не нужен Google
client secret, Gemini API key или OAuth redirect через backend.

## 1. Однократная настройка Google Cloud — действия владельца аккаунта

1. Создать отдельный Google Cloud project для локальной разработки. В Google
   Auth Platform заполнить Branding и Audience, выбрать External / Testing и
   добавить Google-аккаунт на телефоне в test users, если консоль этого требует.
   Использовать только базовый вход (`openid`, email, profile), без дополнительных
   Google API scopes. Не публиковать приложение ради этого теста.
2. В Android-репозитории выполнить `./gradlew :app:signingReport` и взять **SHA-1
   сертификата варианта debug**. Это публичный отпечаток, не приватный ключ.
   Создать OAuth client типа **Android** с package name
   `by.mia.assistant.debug` и этим SHA-1. При смене debug-ключа или машины
   регистрацию нужно сверить заново.
3. В том же Google Cloud project создать OAuth client типа **Web application**.
   Скопировать его **client ID**; именно он передаётся в Android
   `setServerClientId` и в backend `GOOGLE_CLIENT_ID`. Android client ID в эти
   поля не подставлять. В текущем native ID-token flow ngrok URL не является
   OAuth redirect URI: приложение получает ID token через Credential Manager,
   а backend проверяет его audience и nonce.

Официальные источники: [Google Credential Manager](https://developer.android.com/identity/sign-in/credential-manager-siwg-implementation),
[настройка двух OAuth-клиентов](https://codelabs.developers.google.com/sign-in-with-google-android),
[SHA-1 через signingReport](https://developers.google.com/android/guides/client-auth),
[тестовая аудитория](https://support.google.com/cloud/answer/15549945).

## 2. Поднять локальный backend с миграциями

Команды ниже — из корня `mia-helper` на Mac с Docker. Они не используют `.env`.
`docker-compose.yml` уже содержит локальные PostgreSQL/Redis, но backend-образ
не копирует Alembic-файлы, поэтому в одноразовый migration-контейнер они
монтируются read-only. Не направлять миграции на пользовательскую/production БД.

~~~bash
export MIA_BACKEND_PORT='127.0.0.1:8000'
docker compose build backend
docker compose up -d postgres redis
docker compose run --rm \
  -v "$PWD/alembic.ini:/app/alembic.ini:ro" \
  -v "$PWD/migrations:/app/migrations:ro" \
  backend uv run --no-dev alembic upgrade head
~~~

В той же оболочке задать **публичный** Web client ID и один **локальный
секрет** подписи JWT; не вставлять секрет в чат или репозиторий. Сохранить этот
секрет между рестартами backend, пока проверяется восстановление сессии. Длина
`AUTH_JWT_SECRET` должна быть не менее 32 байт. Команда генерации ниже не
печатает значение; для длительной работы хранить его в локальном менеджере
секретов, а не в файле проекта.

~~~bash
export GOOGLE_CLIENT_ID='<WEB_OAUTH_CLIENT_ID>'
export AUTH_JWT_SECRET="$(openssl rand -hex 32)"
docker compose run --rm --service-ports \
  -e GOOGLE_CLIENT_ID -e AUTH_JWT_SECRET \
  -e AUTH_ACCESS_TOKEN_TTL_SECONDS=600 \
  backend
~~~

Последняя команда держит backend в foreground; открыть второй терминал.
Порт 8000 должен быть свободен: не запускать одновременно второй backend
через обычное `docker compose up`.
`AUTH_ACCESS_TOKEN_TTL_SECONDS=600` — допустимый минимум для проверки refresh
через примерно 10 минут. Без него локальный default — 15 минут. Проверить
только безопасный health endpoint:

~~~bash
curl -fsS http://127.0.0.1:8000/health/ready
~~~

Ожидается `{"status":"ok"}`. При 503 сначала проверить контейнеры и миграцию.
Обычное `docker compose up --build` **не** передаёт `GOOGLE_CLIENT_ID` из shell
в сервис: для этого runbook нужен запуск выше с `-e GOOGLE_CLIENT_ID`.

## 3. Временный HTTPS через ngrok

Установить/авторизовать ngrok agent по [официальной инструкции](https://ngrok.com/use-cases/share-localhost);
authtoken остаётся только на вашей машине. В третьем терминале:

~~~bash
ngrok http 8000 --inspect=false
~~~

Скопировать выданный `https://…ngrok…` URL **без завершающего слеша** как
`MIA_PUBLIC_BACKEND_URL`, затем проверить `/health/ready` через него. URL может
измениться при новом туннеле — тогда приложение нужно пересобрать. Для
бесплатного плана ngrok выдаёт dev-домен; пользовательский домен не обязателен
([ограничения](https://ngrok.com/docs/pricing-limits/free-plan-limits)).

Туннель делает локальный API доступным из интернета. `--inspect=false`
отключает локальную HTTP-инспекцию агента, но не заменяет доверие к провайдеру
туннеля. Использовать только тестовый аккаунт, не публиковать URL, не включать
дамп HTTP body/заголовков и закрыть туннель после проверки. Не ставить
дополнительный ngrok OAuth/basic-auth перед этим API: текущий Android клиент
не отправляет такой заголовок и не обрабатывает HTML-redirect на уровне API.

## 4. Собрать и установить liveDebug

Во втором терминале, из `mia-helper-android`, с подключённым телефоном:

~~~bash
export MIA_PUBLIC_BACKEND_URL='https://<your-ngrok-domain>'
export GOOGLE_CLIENT_ID='<тот-же-WEB_OAUTH_CLIENT_ID>'
adb devices -l
./gradlew :app:installLiveDebug \
  -PmiaBackendBaseUrl="$MIA_PUBLIC_BACKEND_URL/" \
  -PmiaWebClientId="$GOOGLE_CLIENT_ID" \
  --console=plain
adb shell am start -n by.mia.assistant.debug/by.mia.assistant.MainActivity
~~~

Оба `-P` значения публичны; токены, client secret и `AUTH_JWT_SECRET` в Gradle
не передавать. Если экран всё ещё показывает `Authentication needs app
configuration`, пересобрать **liveDebug** с обоими непустыми параметрами.
`mockDebug` не проверяет Google OAuth.

## 5. Что проверить и чем закрыть S21-R2

- На телефоне нажать **Sign in with Google**, выбрать тестовый аккаунт и
  убедиться, что открылся экран conversations. Не сохранять скриншоты с email,
  ID token, access/refresh token или тела запросов.
- Завершить процесс приложения и открыть снова: сохранённая Keystore-сессия
  должна восстановиться. После истечения 10-минутного access token открыть
  снова и проверить refresh без нового Google consent.
- Проверить logout и повторный вход. **Сейчас есть отдельный UI-хвост:**
  `AuthRoute` содержит sign-out, но `MiaApp` удаляет auth-экран из back stack
  после входа, а доступного перехода к logout на protected-экранах нет.
  S21-R2 должен добавить достижимое действие выхода и выполнить этот пункт на
  телефоне; до этого S21 остаётся PARTIAL.
- Не считать S21 завершённой, пока не пройдут sign-in, restore, refresh,
  backend revocation/logout и re-login на том же debug-клиенте. Результат
  записать в новый handoff, не в логи с токенами.

Если Google picker не появляется, сначала сверить Android package/SHA-1 и
Web client ID в одном Cloud project. Если Google шаг проходит, но API отвечает
ошибкой, проверить доступность ngrok `/health/ready`, `GOOGLE_CLIENT_ID` в
процессе backend и отсутствие лишнего `/v1` в `miaBackendBaseUrl` — клиент сам
добавляет `/v1/...`. При смене URL туннеля переустановить liveDebug.
