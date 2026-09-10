# MIA contract and governance baseline

Status: accepted for PoC implementation
Owner: S00; later changes require the owning session or a wave integration pass

## Product identity assumption

Until a production domain and OAuth identity are approved, Android uses
`by.mia.assistant` for both `namespace` and `applicationId`. This is a
development-only identifier. S20 must not create release OAuth clients, publish
an artifact, or treat domain ownership as established from this assumption.
The final choice remains tracked by OI-ARCH-001.

## Contract artifacts

| Boundary | Source of truth | Compatibility rule |
| --- | --- | --- |
| REST/SSE | `contracts/openapi/v1/openapi.json` | Additive changes stay in v1; removals or semantic incompatibilities require a new API version. |
| Realtime JSON control | `contracts/realtime/v1/*.schema.json` | Unknown additive payload fields are rejected in v1; compatible capabilities are negotiated in `session.configure`. Breaking changes require protocol v2. |
| Realtime binary audio | `contracts/realtime/v1/README.md` | PCM parameters are negotiated before audio; changing frame interpretation requires a new protocol version. |
| Provider abstraction | `contracts/ai/v1/ai-event.schema.json` | Google/provider types never cross this boundary. New event kinds are contract changes and need fixtures. |
| Public errors | `contracts/errors/v1/error-codes.json` | Existing code meaning and retryability are stable in v1. New codes are additive. |

Contract examples are executable fixtures, not informal documentation. Any
schema change must update affected fixtures and pass
`python contracts/scripts/validate_contracts.py`.

## Ownership

- S00 owns the initial contract baseline.
- S13 consumes and may propose normalized text/tool event changes.
- S14 owns the concrete REST/SSE implementation while preserving OpenAPI v1.
- S15 owns the realtime gateway implementation while preserving realtime v1.
- S16 consumes AIEvent and may propose provider-adapter additions.
- Cross-session breaking changes are applied by the next integration pass after
  recording their consumer impact in the originating handoff.
- A worker does not silently modify another active session's shared contract.

## Git naming

- Default branch: `main`.
- Worker branch: `session/Sxx-kebab-topic`, for example
  `session/S15-realtime-gateway`.
- Repair branch: `session/Sxx-Rn-kebab-topic`.
- Wave integration branch: `integration/Wn`.
- Commit subject: `<type>(<scope>): <imperative summary> [Sxx]`, using one of
  `feat`, `fix`, `test`, `docs`, `build`, `ci`, or `chore`.
- Keep implementation and coordination closeout as separate commits when that
  makes the tested implementation SHA unambiguous.

S00 is the bootstrap exception and runs on `main`; every later worker session
uses a unique branch/worktree as required by the execution plan.

## Test naming

- Python files: `test_<unit>.py`; tests:
  `test_<subject>_<condition>_<expected_result>`.
- Kotlin/JVM tests: `<Subject>Test` and descriptive backtick method names.
- Contract fixtures: lowercase kebab-case event names, or an exhaustive
  `<direction>-events.json` collection whose entries each carry their event
  `type`.
- E2E tests: `<journey>_<provider>_e2e`, and must make fake versus external smoke
  execution explicit.

## Time, identifiers, and idempotency

- JSON timestamps are RFC 3339 UTC strings; storage uses UTC.
- Public resource identifiers are server-generated UUIDs in the v1 baseline.
- `Idempotency-Key` is required for state-changing REST operations. A repeated
  key with the same canonical request returns the original result; reuse with a
  different request produces `idempotency_conflict`.
- Realtime client and server sequence numbers are monotonically increasing in
  independent directions and start at 1 for a new WebSocket connection.

## Secret boundary

`.env` and its variants are ignored at every directory depth, while
`.env.example` remains trackable. Contract tests only inspect files under
`contracts/`. No secret value may appear in fixtures, documentation, logs,
handoffs, or commits.
