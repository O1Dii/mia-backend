# ADR-0019: Personal Node — the same core on the user's computer, paired by QR, reached through an end-to-end TLS relay

- Status: **Proposed** (needs the user's decisions Р1–Р12 below; M86 starts after them)
- Date: 2026-10-11
- Proposed by: M85 (Claude Opus 5.5)
- Scope (user decision 2026-10-11): the first version must cover all four use cases, before MW7 (pilot):
  1. local models (Ollama);
  2. files and actions on the PC;
  3. memory and history stored at home;
  4. «node instead of server».
  Nodes appear in «Устройства» (M84).
- Sources:
  - [Mia_Architecture.md](../Mia_Architecture.md) §5–6, §11, §18;
  - [Mia_Architecture_Review.md](../Mia_Architecture_Review.md) §7, §10, §12;
  - [Mia_Backlog.md](../Mia_Backlog.md) §2–3;
  - research: [docs/research/personal-node/](../research/personal-node/README.md) — options with sources, Postgres
    inventory, integration points;
  - draft protocol: [contracts/node/v1](../../contracts/node/v1/README.md).
- Related:
  - ADR-0011 (run profiles, limits, `uncertain`);
  - ADR-0012 (memory, DEK);
  - ADR-0013 («Войти через Mia»);
  - ADR-0016 (device policy; this ADR proposes one amendment, §5);
  - ADR-0017 (chats).

## Context

Mia runs in the cloud: an API and a worker on one VM, with PostgreSQL and Redis. Phones are devices: they act and
read data only on request and with consent on the device (ADR-0016). The user wants a Personal Node — Mia on a home
computer — with all four capabilities in its first version. The architecture documents already fix four things:

1. The node opens an outbound connection only; the home router stays closed.
2. Pairing uses a QR code and device keys.
3. Content between clients and the node is encrypted over the relay with a vetted protocol. Mia designs no crypto of
   its own.
4. If the PC is off, work waits (`waiting_for_node`) and never moves to the cloud silently.

Facts found by M85 (details in the research folder):

- **Storage.** The core is tied to PostgreSQL in about 190 places across about 27 files: upserts, 47 row locks,
  SKIP LOCKED queues, JSONB, FTS, regex CHECKs and 110 timezone-aware columns. The 18 migrations are Postgres-only.
  There is no repository layer.
- **Redis** sits behind 4 methods and is cheap to replace.
- **Ollama.** It is wired as a chat-only, loopback-only provider. Ollama itself offers an OpenAI-compatible
  `/v1/chat/completions` with streaming and tools.
- **In-process state.** Device consent, the device channel and run execution live in one process.
- **TLS.** The test server sits behind a provider TLS gateway that terminates TLS for `mindmapgen.xyz`.

## Decision (proposed)

### 1. Form: one `mia-node` process from this repository

- The node is the same Python core (`app/`) plus a node package `app/node/**` (agent, pairing, control channel,
  tunnel, local tools, Ollama bridge, embedded-Postgres supervisor), started as `mia-node`.
- It is installed with `uv tool install` / `pipx` and runs under launchd / a Windows scheduled task or service /
  systemd. A Docker Compose variant serves home Linux servers.
- Code `N` lives in this repository (repo key B), so cloud and node share one core, one migration chain and one
  test suite.
- **Lightweight mode** (`data_mode: cloud`): the process only runs the agent — control channel, tools, Ollama
  bridge — with no database.
- **Full mode** (`home`, `standalone`): the same process also serves the core API and runs the worker loops in its
  lifespan (one process, one writer).

### 2. Storage: embedded PostgreSQL (Р1)

- `mia-node` manages a private PostgreSQL 16 cluster. It uses pinned binaries (zonky embedded-postgres-binaries or
  EDB), verified by SHA-256, with `initdb` on first start and listening only on `127.0.0.1` or a socket with a random
  password.
- The node runs the same `alembic upgrade head` as the cloud.
- Redis is replaced by an in-process implementation of `app/core/redis.py`'s 4 methods.
- A major-version upgrade goes through the logical archive (§8), which is needed for migration anyway.
- SQLite was rejected for v1: the port is large, and it adds a permanent second dialect and test matrix. Every
  migration would be written twice, and node-only bugs would surface only at users. It stays a post-pilot option.

### 3. Pairing, keys, revocation (M86)

- **Device key.** The node has an Ed25519 device key in the OS key store (file 0600 on headless Linux).
- **Pairing** (`contracts/node/v1/pairing.schema.json`):
  1. The node registers its public key with a proof-of-possession signature.
  2. The cloud returns a pairing id, a 10-minute code `XXXX-XXXX` and a poll token.
  3. The node shows a QR (id, code, key fingerprint, API host).
  4. The Android app scans it, or the web «Устройства → Добавить компьютер» takes the code and the user compares the
     fingerprint.
  5. The cloud binds the key to the account only if the fingerprints match.
  6. The node polls with signed requests until it is claimed.
  - New nodes start in `cloud` mode. Rate limits: per IP for start; 5 wrong codes per account per hour.
- **Control channel login.** No bearer secret is stored on the node. Each connection starts with a challenge–response:
  a single-use 30-second nonce and a domain-separated signature bound to the connection id.
- **Revocation** («Отключить» in «Устройства», or `mia-node unpair`):
  - the node is marked `revoked`;
  - the live channel gets a `revoked` frame and close 4403, and further connections with the key are refused;
  - ADR-0016 grants for the node are deleted;
  - its certificate is revoked and its DNS name removed.
  - The node erases its key and relay credentials. Data at home stays on the disk, and the node's screen says so.
- **Key rotation.** `node.rotate_key` is signed by both the old and the new key.

### 4. Control channel: outbound WSS, sequence numbers, replay protection, presence (M86)

- `wss://<api>/v1/nodes/connect` carries JSON frames (`envelope.schema.json`):
  - handshake: `challenge` / `hello` / `welcome`;
  - `ping` / `pong` every 25 s;
  - `manifest`;
  - `rpc.request` / `rpc.stream` / `rpc.response` / `rpc.cancel`;
  - `revoked` / `close`.
- **Sequence numbers.** Session frames carry a per-direction `seq` that starts at 1, grows by exactly 1 and survives
  a reconnect. `ack` is the highest contiguous seq received.
  - Duplicates after a resume are dropped. A gap is a protocol error: reconnect and resume.
  - A resume that cannot be honoured (more than 256 frames or 60 s) starts a fresh session. Every in-flight call then
    fails: reads `failed`, actions `uncertain`, never retried (ADR-0011).
- **RPC.** Each RPC id is an idempotency key. The node keeps executed tool-call ids for 24 h, so an action never runs
  twice. Requests carry `deadline_at`. Tool calls carry `args_hash`, which the node recomputes.
- **Presence.** A node is *online* while this channel is open and heartbeats arrive. When it closes, `last_seen_at`
  is the close time. M84 shows this.
- The channel is terminated by the API process that runs runs. A multi-process cloud needs routing between
  processes (OI-M85-004).

### 5. Routing and consent: the node is one more device in ADR-0016 (M87)

- The node is a `devices` row (`platform: node`) plus a `nodes` row. It therefore reuses, unchanged:
  - the policy `allow | ask | deny` over (capability, requester, target, scope);
  - per-run and 30-day persistent grants;
  - capability switches («others may ask / never»);
  - the access log;
  - `/v1/devices/{id}/grants|access-log|capabilities`.
- **Tools** (risk and capability kind):

  | Tool | Risk | Capability |
  | --- | --- | --- |
  | `node.files.search` | read | `files.search` |
  | `node.files.read` | read | `files.read` |
  | `node.apps.open` | write_reversible | `apps.open` (action) |
  | `node.links.open` | write_reversible | `links.open` (action) |

  v1 has no write, delete or external actions on the PC. Results carry names and labels, never absolute paths. File
  references are opaque `file:` refs valid for 24 h.
- **Local envelope.** Which folders, which apps and which capabilities are on is configured **only on the node**
  (`mia-node folders add …`, local settings); the cloud cannot widen it. Secret-looking files are denied by default:
  `.env*`, `.ssh`, keys, password databases, browser profiles.
- **Where consent is given (Р4).** By default on the owner's phone, the node's designated approver. It uses the M71
  consent sheet and push:
  - «Домашний ПК: поиск файлов в «Документы» — просит браузер Chrome на macOS»;
  - the approval carries `device_consent.waiting_for: "node"` (additive);
  - only the approver device may decide.
  - A headless server has no screen and a PC usually has nobody in front of it, so this **amends ADR-0016 §4** for
    nodes only. Consent on the node itself (desktop notification + local page) can be added later as `approver: node`.
- **Node offline.**
  - An interactive run waits ≤ 30 s for a reconnect, then the tool or model step fails with `node_offline` and the
    model says so plainly.
  - A job targeting a node is re-queued (`run_after`) until the node returns, at most 12 h by default (Р5). It then
    fails and notifies the user.
  - Nothing ever silently degrades to the cloud or another device. A cloud fallback exists only where the user
    configured it explicitly.

### 6. Local models: Ollama behind the Model Gateway (M88)

- The node discovers Ollama on `127.0.0.1:11434` and reports installed models with capabilities (`tools`, `vision`,
  …) in the manifest.
- **Cloud runtime.** A new adapter `node-ollama` sends `model.generate` over the control channel. The node calls
  Ollama `/v1/chat/completions` with `stream: true` and relays normalised deltas (`rpc.stream`). The adapter turns
  them into `TextDelta` / `ToolCallRequested` / `ModelUsage` / `ModelDone`.
- **Runtime on the node.** The existing `ollama` provider moves from chat-only to the OpenAI-compatible
  chat-completions adapter with streaming and tools, still loopback-only.
- **Limits (ADR-0011).**
  - The run engine starts passing a per-profile `max_output_tokens`.
  - The node enforces `deadline_at`, `first_token_timeout_ms`, `rpc.cancel` and one generation at a time
    (`node_busy`).
  - Voice sends `model.warmup` when the voice screen opens and keeps the model loaded, to approach the ~6 s
    first-sound target.
- **Selection and fallback.** «Модели» lists «Модель на ПК: <name>». A node model is never part of the automatic
  `mia:default` fallback chain. If the user made it primary and the node is off, the step fails with `node_offline`.
  An explicit «если ПК выключен — облачная модель» switch is the only fallback.
- **Honest wording.** In `cloud` mode the cloud runtime builds the prompt, so **Mia's server sees the text**. A local
  model protects from a third-party provider, not from the operator. Full privacy comes with `home` / `standalone`.

### 7. Data at home: `data_mode` per account (M89)

| Data | `cloud` | `home` | `standalone` |
| --- | --- | --- | --- |
| Identity (Google `sub`, e-mail), cloud sessions | cloud | cloud | node (local owner; cloud account optional) |
| Device and node registry, public keys, push tokens | cloud | cloud | node (+ cloud registry only if relay or push are used) |
| Relay routing, DNS, certificate records | — | cloud | cloud (if relay is used) |
| Chats, runs, events, summaries | cloud | **node** | **node** |
| Memory (facts, people, preferences, access log) | cloud | **node** | **node** |
| Jobs, schedules, inbox | cloud | **node** | **node** |
| Plugin installations, state, plugin OAuth tokens | cloud | **node** | **node** |
| Model keys and connections | cloud (DEK) | **node** (OS key store) | **node** |
| Cloud metadata about content | — | none: no titles, no counts; only «data home = node X» and last contact | — |

- **Clients in `home` mode.**
  - `GET /v1/me` tells clients the data home (`node_id`, `https://<host>.<zone>`, SPKI pin).
  - Android switches its base URL to the node and pins the node's TLS key. The pin comes from the manifest, signed by
    the device key whose fingerprint was compared at pairing.
  - The web opens the node's origin, which serves the same web build from the node, so the cloud cannot inject
    JavaScript there.
  - Clients authenticate to the node with «Войти через Mia» (ADR-0013). The node is an OAuth client and verifies the
    ES256 ID token and the owner's `sub`. The Android app gets a short-lived cloud-signed enrollment assertion.
  - The node then issues its own sessions and refresh families (the existing M10 code).
- **Node off.**
  - Clients show «Компьютер не в сети — история и память недоступны» (`node_data_unavailable`).
  - A temporary cloud chat without memory exists only behind an explicit, labelled button.
  - Jobs run on the node and use the M16 misfire policies after it returns.
  - Reminders due while it is off: Р6.
- **Sync.** One writer: the node. There is no multi-master. Clients only cache. Moving between modes is an explicit
  migration (§8).
- **Backup.**
  - A daily `pg_dump` plus workspace files, encrypted with **age** (vetted format; pyrage), into a user-chosen folder:
    an external disk, a NAS or a cloud-drive folder.
  - The recovery key is shown once and kept in the OS key store.
  - Retention is 7 daily + 4 weekly copies; `mia-node backup verify` restores into a scratch cluster.
  - An encrypted copy in Mia's cloud: Р10.
- **Plugin OAuth.** Callbacks reach the node directly through the relay name. For clients with a fixed cloud redirect
  URI, the cloud forwards code+state (`oauth.callback`). The PKCE verifier never leaves the node, so the cloud cannot
  redeem the code.

### 8. End-to-end encryption and the relay (M91)

- **Protocol.** TLS 1.3, terminated on the node. Clients connect to `https://<random host>.n.<zone>`, whose DNS
  points to `mia-relay`. The relay reads only the SNI and forwards raw TLS bytes over the node's outbound tunnel
  (binary frames, `contracts/node/v1/README.md`). This is the Home Assistant Cloud (SniTun) pattern, implemented by
  Mia without its own cryptography.
- **Certificate.** The node's Let's Encrypt certificate is issued through `cert.issue`: the node sends a CSR and the
  cloud runs ACME DNS-01 with its DNS credentials. The private key never leaves the node.
- **Infrastructure.** This needs a host with raw TCP 443 (not behind the provider TLS gateway) and a DNS zone with an
  API (Р2, Р3).
- **LAN.** Android may reach the node's LAN address directly with the same SNI and pin. On the PC itself:
  `https://localhost`.
- **Rejected:**
  - Noise over WSS — reserve if Р2 is declined: three implementations, HTTP-over-frames in every client, and the web
    client is still served by the cloud;
  - MLS — overkill;
  - Cloudflare Tunnel — its proxy terminates TLS, so it is not end-to-end;
  - port forwarding / DDNS.
  - Tailscale stays a user option for `standalone`.
- **Archive** (`mia-archive/v1`, M90):
  - `manifest.json` (schema revision = Alembic head, counts, SHA-256 per file) + JSONL per entity. Memory is
    exported decrypted and re-sealed with the node's key.
  - Cloud → node:
    1. freeze the account (`account_migrating`);
    2. the node downloads the archive over TLS from the cloud;
    3. import, verify, smoke;
    4. the user confirms → the cloud switches the data home and crypto-shreds the content (DEK, M17/M17-R1 path);
    5. unfreeze.
  - A failure before step 4 changes nothing. The reverse is `POST /v1/account/archive`.

#### Threat model

| Party | Sees | Cannot | Residual risk and mitigation |
| --- | --- | --- | --- |
| Relay / Mia operator in `home` / `standalone` | Node name (SNI), client IPs, sizes, timing, online times | Read content (TLS ends on the node) | Controls DNS, so could mis-issue a certificate and MITM **browsers**. Android pins the node key from the node-signed manifest; the node may watch CT logs (later); the privacy page says this honestly |
| Mia cloud runtime in `cloud` mode | Everything it processes: prompts sent to Ollama on the PC, file text returned by node tools | Widen the node's local envelope (folders, apps, capabilities) | Consent per ADR-0016; results minimised (no paths); persistent grants revocable; journal on the node |
| Cloud model provider | Only what a run sends to it. In `home` with Ollama: nothing. With a cloud model in `home`: the prompt, sent from the node with keys stored on the node | — | Provider retention shown in «Модели» (ADR-0012) |
| Compromised web client served from the cloud | What it displays (`cloud` mode) | In `home`, the web is served by the node; cloud-served JS cannot reach node content without the MITM above | Signed releases (Backlog §3) |
| Stolen phone or browser session | What that client may do | Read anything beyond the node's local envelope | Revoke the device in the cloud and on the node; consent per ADR-0016; screen lock |
| Malicious device in the LAN | — | Reach Ollama (localhost only) or the node API without a session | The node exposes pairing only on localhost |
| Malware on the PC | Everything on the PC | — | Out of scope; revoke the node from «Устройства» |
| Prompt injection inside a read file | — | Trigger actions without approval | Tool output is untrusted data (ADR-0011); actions need consent; no external actions in v1 |

### 9. «Node instead of server» (`standalone`, M90)

- **Sign-in (Р7):**
  - the owner pairs locally by a one-time code or QR from the node's own screen;
  - Android then uses its device key and a node refresh family;
  - browsers use passkeys (WebAuthn on the node's real HTTPS name);
  - a Mia account stays optional, only to use the relay and the push proxy;
  - Google sign-in directly on the node is not offered: the user would need their own Google Cloud OAuth client.
- **Push (Р8).** Content-free wake pushes through Mia's FCM project (`push.send`); the app fetches from the node. FCM
  credentials are Mia's secret and cannot be given to nodes.
- **Telegram (Р9).** The user's own bot (BotFather token on the node) with long polling `getUpdates`, so no public
  endpoint is needed.
- **Outside home.** Through the Mia relay (end-to-end TLS) by default; Tailscale as an option; LAN directly.
- **Store.** The catalog of public packages is downloaded from the cloud when reachable and cached.
- **Archive.** Migration and backup as in §7–8. The install guide is part of M90.

### 10. «Устройства» (M84)

- A node is listed in `GET /v1/devices` as `kind: node`, with the fields of `NodeDeviceView` in
  `capability-manifest.schema.json`:
  - name, OS, version;
  - online / last seen, from the control channel;
  - data mode;
  - key fingerprint;
  - capabilities with status (enabled / disabled / unavailable + reason) and the ADR-0016 remote setting.
- Persistent grants and the access log use the existing per-device endpoints.
- Actions: rename, «Отключить» (= revoke, §3).
- «Облако Mia» stays a separate row. M86 maps this projection onto M84's device schema; M85 does not edit M84's
  files.

### 11. Phasing

| Session | Content | Depends on |
| --- | --- | --- |
| M86 | Lightweight node, pairing, control channel, presence and revocation in «Устройства» | M85 + Р1–Р4 + M84 |
| M87 | Files and actions on the PC | M86 |
| M88 | Ollama | M86 |
| M91 (new) | Core on the node (embedded Postgres, no Redis, one process) + end-to-end TLS tunnel (relay, certificates) | M86 + Р2/Р3 |
| M89 | Data at home | M91 |
| M90 | Node instead of server + migration + backup + install guide | M89 |

M87, M88 and M91 can run in parallel. Refined cards are in `docs/implementation/handoffs/M85.md`.

## Consequences

- New contract family `contracts/node/v1` (draft, validated). Public REST paths and error codes go into
  `contracts/mvp/v1` in M86 (after M84 merges).
- **ADR-0016 amendment for nodes (if Р4 = a).** The approver device decides for a node. The local envelope on the
  node bounds what any consent can reach.
- **Infrastructure.** A relay host with raw TCP 443, a DNS zone with an API, and ACME automation.
  Architecture §18 already anticipated a separate relay VM.
- The test stand gains a second process type (the node) in e2e tests: an in-process relay with a test CA, and a
  scripted Ollama.
- MW6b's gate («сквозные тесты») runs M60 scenarios with the node in `home` and `standalone` modes.

## Решения для пользователя (Decisions for the user)

### Ответы пользователя 2026-10-11

- **Р1 — а:** встроенный PostgreSQL (Docker — дополнительно для Linux-серверов).
- **Р2 — а:** TLS до узла через relay. **До production-релиза relay работает на том же сервере, где развёрнуто всё
  остальное** (mindmapgen.xyz); отдельный сервер — к production. M91 проверяет, можно ли на текущем сервере пропустить TLS
  без расшифровки (SNI-проброс в nginx `stream` на 443 или отдельный порт, если 443 терминирует шлюз провайдера — OI-M85-002).
- **Р3:** в production — отдельный короткий домен; **для текущих тестов — поддомен mindmapgen.xyz** (`*.n.mindmapgen.xyz`),
  если его DNS позволяет wildcard и выпуск сертификатов (DNS-01 через API или HTTP-01 через relay — решает M91).
- **Р4 — выбор пользователя, а не фиксированный вариант.** Personal Node нужен, чтобы пользователь чувствовал безопасность
  и контроль и не думал, что что-то происходит без его ведома. Поэтому:
  - где подтверждать действия на узле — **настройка пользователя на самом узле**: на телефоне, на этом компьютере, на любом
    из них (что раньше), или «не спрашивать» для отдельной возможности/папки; при первой привязке узел **спрашивает** этот
    выбор явно, без тихого значения по умолчанию;
  - настройка меняется только на самом узле (окно узла M93; до него — команда `mia-node` и первый запуск), облако и другие
    устройства её не меняют;
  - **журнал узла включён по умолчанию:** каждый запрос к узлу — кто и откуда спросил, какая возможность, какие файлы
    прочитаны/какие программы открыты, решение и кем оно принято. Журнал хранится на узле и виден пользователю в его
    истории (окно узла «История запросов» и карточка узла в «Устройствах» M84). Пользователь может выключить журнал или
    ограничить срок хранения ради места — только на самом узле, с явным предупреждением.
  - Это уточняет ADR-0016 §4 для узлов: согласие даётся там, где выбрал владелец узла, а не только на целевом устройстве.
- **Р11 — б:** установщики (M92), окно узла — позже (M93); ПК пользователя — macOS; подпись кода пока не нужна.
- Остальные (Р5–Р10, Р12) — ниже, ждут ответа.

Каждое решение — варианты и рекомендация. Обязательны до M86: Р1–Р4. Остальные нужны до сессии, указанной в скобках.

**Р1. На чём узел хранит данные** (до M86)
- а) **Встроенный PostgreSQL**, которым управляет `mia-node`: тот же код и те же миграции, что в облаке; +1 фоновый
  процесс на ПК (~30–60 МБ памяти).
- б) SQLite: один файл, но большой перенос кода (~190 мест) и постоянная двойная поддержка.
- в) Docker с теми же образами: только для домашних серверов на Linux.
- **Рекомендация: а** (в — дополнительно для серверов).

**Р2. Как телефон и браузер достают узел вне дома с шифрованием «от клиента до узла»** (до M86 — влияет на M91)
- а) **TLS до самого узла через relay Mia**. Так сделан удалённый доступ Home Assistant. Браузер работает без
  изменений. Нужен отдельный маленький сервер с «сырым» портом 443 (≈ 4–6 €/мес) или проверка, умеет ли текущий
  провайдер пропускать TCP без расшифровки.
- б) Шифрование Noise поверх нынешнего соединения: ничего нового в инфраструктуре, но много кода в web и Android,
  и web-клиент по-прежнему грузится из облака.
- в) Только Tailscale у пользователя.
- **Рекомендация: а**; если отдельный сервер недопустим — б.

**Р3. Домен для адресов узлов** (до M91). Узлы получают адреса вида `nq4m7x2kd5rbt6wya.n.<домен>` с сертификатами
Let's Encrypt, и DNS этого домена должен управляться через API.
- а) **Поддомен существующего домена**, если его DNS-провайдер даёт API.
- б) Отдельный короткий домен (≈ 10 €/год) на DNS с API, например Cloudflare DNS (только DNS, без прокси).
- **Рекомендация: б** — не смешивать с MindWeaver и лимитами сертификатов основного домена.

**Р4. Где подтверждать действия на ПК** (до M86; меняет ADR-0016 §4 для узлов)
- а) **На телефоне владельца** (тот же экран согласия, что сейчас для телефона): работает и для сервера без экрана.
- б) Только на самом ПК — уведомлением; работает, только если вы сидите за компьютером.
- в) И там, и там: что раньше.
- Во всех вариантах папки и программы, к которым вообще можно обращаться, выбираются только на самом ПК.
- **Рекомендация: а сейчас, в — позже.**

**Р5. Если ПК выключен, а фоновая задача должна работать с ним** (до M87)
- а) **Ждать до 12 часов, затем сообщить об ошибке.**
- б) Ждать сколько угодно.
- в) Не ждать.
- Отката в облако без вашего явного разрешения не будет ни в одном варианте.
- **Рекомендация: а.**

**Р6. Напоминания и таймеры в режиме «данные дома», когда ПК выключен** (до M89)
- а) Не срабатывают, пока ПК выключен; после включения — по правилам пропуска.
- б) **Переключатель «Напоминания через облако»**: время и текст напоминаний хранятся и в облаке, только они.
- в) Телефон хранит копию ближайших напоминаний (доработка Android).
- **Рекомендация: б** (по умолчанию выключен, с предупреждением на экране), позже в.

**Р7. Вход в режиме «узел вместо сервера»** (до M90)
- а) **Без облака**: первое устройство подключается кодом с экрана ПК; дальше телефон входит своим ключом, браузер —
  ключом доступа (passkey).
- б) Аккаунт Mia через Google остаётся, пока доступно облако.
- в) Вход Google прямо на узле: нужен свой проект Google Cloud.
- **Рекомендация: а** (+ б как опция, если вы пользуетесь relay и push Mia).

**Р8. Push-уведомления в режиме «узел вместо сервера»** (до M90)
- а) **Через сервер Mia без содержимого**: телефон просыпается и забирает данные у ПК.
- б) Без push: уведомления, только пока приложение открыто.
- в) Свой сервер ntfy (UnifiedPush).
- **Рекомендация: а.**

**Р9. Бот Telegram в режимах «дома» и «вместо сервера»** (до M89)
- а) **Ваш собственный бот** (токен от BotFather хранится на ПК): сообщения не проходят через сервер Mia.
- б) Бот Mia: текст проходит через облако.
- в) Выключен.
- **Рекомендация: а.**

**Р10. Резервные копии узла** (до M90)
- а) **Зашифрованная копия в выбранную вами папку**: внешний диск, NAS, папка Google Drive или Яндекс Диска.
- б) Плюс зашифрованная копия на сервере Mia (место на сервере, ключ только у вас).
- в) Без бэкапа.
- **Рекомендация: а** (б — после пилота).

**Р11. Как ставить `mia-node`** (до M86)
- а) **Командой `uv tool install mia-node`** и сервисом автозапуска; для Linux-серверов — Docker.
- б) Сразу установщики для Windows и macOS (дольше; подпись кода стоит денег).
- **Рекомендация: а для пилота**, б — после.
- **Решение пользователя 2026-10-11: б — установщики** (Windows, macOS, Linux) и своё окно узла (данные, история запросов);
  окно — не в первой версии. Установщики — сессия M92, окно узла — M93. `uv tool install` остаётся способом для разработки и CI.
  Платная подпись кода — отдельный вопрос пользователю (OI-M85-011).

**Р12. Сколько узлов на аккаунт** (до M87)
- а) **Несколько** (ПК + домашний сервер): модель выбирает узел по имени.
- б) Один на аккаунт в первой версии.
- **Рекомендация: а** — переделывать позже дороже.
