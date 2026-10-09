# ADR-0015: The MVP plan supersedes sessions S28, S31-R1 and S33

- Status: Accepted
- Date: 2026-10-09
- Sources: [Review](../Mia_Architecture_Review.md) §0, §6, §11 («Голос»,
  «Harness», «SDK»); [MVP plan](../implementation/MVP_SESSION_PLAN.md) §1
- Related: ADR-002 (backend-mediated realtime), ADR-005 and ADR-009 (wake word)

## Context

The legacy plan (`SESSION_EXECUTION_PLAN.md`, sessions `Sxx`, waves `W0…W7`)
assumed a realtime speech-to-speech model (Gemini Live) as the voice path, a
system-assistant spike before beta, and a final beta audit of that design.
The architecture review chose variant E: on-device STT (GigaAM), any LLM,
then TTS (a cascade), Android as the default system assistant
(VoiceInteractionService), and an MCP-native core with an app SDK. Three open
legacy sessions no longer describe work that should be done.

## Decision

| Legacy session | New status | Replaced by |
| --- | --- | --- |
| S28 — system assistant spike | `SUPERSEDED` | M20 (Android VIS) |
| S31-R1 — voice E2E repair on Gemini Live | `SUPERSEDED` | M22 (cascade voice) and M60; Gemini Live becomes optional |
| S33 — final beta audit and release gate | `SUPERSEDED` | M60 (end-to-end MVP acceptance) |
| S27 — foreground wake phrase | `PARTIAL / DEFERRED` | wake word moves to the backlog (ADR-009 stands) |
| S32 — tool E2E | `PARTIAL` | kept as a historical record |

1. New work is planned only as `Mxx` sessions in waves `MW0…MW7`; IDs do not
   collide with `Sxx` / `W0…W7`.
2. Code and handoffs of legacy sessions stay as the base and are not rewritten
   without need. Open legacy issues move to the M-sessions listed in
   MVP plan §8 (for example OI-W6-005 and OI-W6-017 → M22, OI-W6-013 → M11).
3. ADR-002 remains valid for the optional realtime path; the default voice
   path is the cascade over the run API (ADR-0011).

## Consequences

- The MW0 integrator records these statuses in `STATUS.md`; this ADR is the
  rationale it references.
- Legacy gates W6/W7 are not run; MW-gates (MVP plan §7) replace them.
- `app/web_poc` remains a migration source until M31 removes it.
