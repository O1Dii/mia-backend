# MIA

MIA is a provider-neutral conversational assistant platform. This repository is
the backend and cross-client contract source of truth; the Android client lives
in a separate sibling repository once S20 creates it.

The current baseline contains governance documents and versioned REST,
realtime, normalized AI event, and error contracts. Backend application code is
introduced by S10.

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
