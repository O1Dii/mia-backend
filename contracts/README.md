# MIA contracts

This directory is the machine-readable boundary shared by the backend and all
clients.

- `openapi/v1/openapi.json`: REST and SSE skeleton.
- `realtime/v1`: WebSocket JSON control schemas, binary framing rules, and
  exhaustive fixtures.
- `ai/v1`: normalized provider event schema and exhaustive fixtures.
- `errors/v1`: stable public error envelope and code registry.
- `node/v1`: Personal Node protocol draft (pairing, control-channel frames, RPC,
  capability manifest), ADR-0019.

Run validation from the repository root:

~~~bash
.venv/bin/python contracts/scripts/validate_contracts.py
~~~

The schemas use JSON Schema Draft 2020-12 and reject unknown event-envelope and
payload fields. A breaking contract requires a new version directory; additive
changes still require consumer review, fixtures, and a handoff entry.
