# MIA

MIA is a platform of useful apps and plugins around your AI models: local
inference or your own cloud API keys. This repository contains the backend,
cross-client contracts, and the new React web POC. The Android client lives
in a separate sibling repository.

The existing FastAPI backend includes auth, persistent conversations, REST/SSE,
Gemini text and realtime, tool policy/audit, and PostgreSQL/Redis integration.
Its remaining Android voice/release gates are tracked in
[STATUS](docs/implementation/STATUS.md).

## Web platform POC

The web app includes a searchable plugin store with detail pages, screenshots and reviews, seven plugins, a customizable dashboard and model-driven
tools, model connections (OpenAI, Anthropic, Gemini, DeepInfra, Groq, Mistral,
Ollama and LM Studio), tool-enabled chat, import/export of app manifests, and an SDK with a downloadable timer app.
Google-only hosted login, persistent account workspaces, browser dictation and text-to-speech are implemented. Payments are not implemented.
Published and verified at [mindmapgen.xyz/mia](https://mindmapgen.xyz/mia/), with a new tile on the shared hub.

- [Run the web POC](web/README.md) — React on localhost:5173, separate local API on 8001.
- [Platform roadmap](docs/WEB_PLATFORM_PLAN.md) — current scope and the next implementation stages.
- [Plugin authoring](docs/PLUGIN_DEVELOPMENT.md) — manifest v2, executable skills, scoped SDK and account integration roadmap.
- [Shared-server deployment](deploy/README.md) — `/mia/`, shared Google OAuth and hub tile.
- [Google login preview](docs/previews/mia-google-login.png), [plugin page](docs/previews/mia-plugin-page.png).
- [Dashboard preview](docs/previews/mia-dashboard-desktop.png), [mobile dashboard](docs/previews/mia-dashboard-mobile.png).
- [Desktop preview](docs/previews/mia-web-desktop.png), [mobile preview](docs/previews/mia-web-mobile.png).

Local development needs no external database or secret-value files. Hosted mode
requires Google authentication and persists account/plugin state in SQLite.
Provider keys stay in API process memory and expire after eight hours or restart.

## Backend local development

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/). The locked
development environment includes Ruff, mypy, and pytest.

~~~bash
uv sync --frozen --all-groups
uv run ruff check .
uv run mypy app tests
uv run pytest
~~~

Run the service without infrastructure to inspect liveness:

~~~bash
uv run uvicorn app.main:app --reload
curl -fsS http://127.0.0.1:8000/health/live
~~~

Start the complete local stack (fake provider, PostgreSQL, and Redis):

~~~bash
docker compose up --build
curl -fsS http://127.0.0.1:8000/health/ready
~~~

If port 8000 is already used, set `MIA_BACKEND_PORT` before starting Compose.
Compose runs Alembic as a one-shot release step before the backend. Internal beta
deployment and backup/restore steps are in [ops/README.md](ops/README.md).

`AI_PROVIDER=fake` is the default. Gemini credentials are optional and must be
provided only through local/deployment configuration; never commit their values.

## Contract verification

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install -r contracts/requirements.txt
.venv/bin/python contracts/scripts/validate_contracts.py
~~~

The validator checks the OpenAPI document, every JSON Schema, exhaustive event
fixtures, and error-code consistency across public transports.

## Session workflow

Implementation is coordinated through `session_plan.md`. A session starts from
a short command such as `Выполни S10`; the executor resolves repositories,
dependencies, branches, verification, and handoffs from the coordination files.

- Contract and ownership baseline: `docs/implementation/CONTRACT_BASELINE.md`
- Current readiness: `docs/implementation/STATUS.md`
- Session handoffs: `docs/implementation/handoffs/`
- Architecture decisions: `docs/adr/`

Never commit local secret-value files. Variable names may be documented later in
`.env.example`, but secret values belong only in local or managed secret stores.
