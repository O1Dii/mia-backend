# MIA

MIA is a provider-neutral conversational assistant platform. This repository is
the backend and cross-client contract source of truth; the Android client lives
in a separate sibling repository once S20 creates it.

The current baseline contains governance documents and versioned REST,
realtime, normalized AI event, and error contracts. The backend scaffold is a
FastAPI modular monolith with PostgreSQL and Redis readiness probes.

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
