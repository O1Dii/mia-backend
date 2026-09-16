# MIA implementation status

Обновляется wave integrator после проверки handoff и commits. Worker sessions не объявляют downstream session READY самостоятельно.

Допустимые состояния: PENDING, READY, IN_PROGRESS, COMPLETED, PARTIAL, BLOCKED, FAILED, SUPERSEDED.

| ID | Wave | Status | Dependencies | Preferred executor | Handoff | Integrated commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S00 | W0 | COMPLETED | — | Fable 5.1 high | [S00](handoffs/S00.md) | b56c17f | contracts/governance baseline verified |
| S10 | W1 | COMPLETED | S00 | Terra high | [S10](handoffs/S10.md) | c7a2846 | backend scaffold verified |
| S20 | W1 | COMPLETED | S00 | Terra high | [S20](handoffs/S20.md) | 065617a | Android scaffold verified; code at 21a31ea |
| S11 | W2 | COMPLETED | S10 | Sol high | [S11](handoffs/S11.md) | 79e1d1d | PostgreSQL persistence verified |
| S13 | W2 | COMPLETED | S00,S10 | Sol xhigh | [S13](handoffs/S13.md) | dd73636 | text provider/orchestration verified |
| S15 | W2 | COMPLETED | S00,S10 | Sol xhigh | [S15](handoffs/S15.md) | 52f1a74 | realtime fake/gateway verified |
| S24 | W2 | COMPLETED | S20 | Sol xhigh | [S24](handoffs/S24.md) | 3b7e8eb | Android main at 9a4e7f3; physical device gate verified |
| S12 | W3 | COMPLETED | S10,S11 | Sol high | [S12](handoffs/S12.md) | c86b0ae | auth mounted; Google dependency pinned |
| S14 | W3 | COMPLETED | S11,S13 | Sol high | [S14](handoffs/S14.md) | c86b0ae | REST/SSE mounted and verified |
| S16 | W3 | COMPLETED | S13,S15 | Fable 5.1 xhigh | [S16](handoffs/S16.md) | c86b0ae | Gemini Live factory integrated; cloud smoke remains S31 |
| S25 | W3 | COMPLETED | S15,S20,S24 | Sol xhigh | [S25](handoffs/S25.md) | c86b0ae | Android main at 6e7395b; physical gate verified |
| S17 | W4 | COMPLETED | S13,S14,S15 | Fable 5.1 xhigh | [S17](handoffs/S17.md) | e9ca3f1 | read-only tool policy/audit integrated; backend gate PASS |
| S21 | W4 | COMPLETED | S12,S20 | Sol high | [S21-R2](handoffs/S21-R2.md) | b7a6c10 | real sign-in/refresh/logout/re-login PASS on SM-S938B |
| S22 | W4 | COMPLETED | S11,S14,S20 | Sol high | [S22](handoffs/S22.md) | e9ca3f1 | Android main 452949c; cache/Room gate PASS |
| S26 | W4 | COMPLETED | S16,S24,S25 | Fable 5.1 xhigh | [S26-R1](handoffs/S26-R1.md) | e9ca3f1 | Android main 7db3e6f; physical flush/cancel 83/84 ms PASS |
| S18 | W5 | COMPLETED | S12,S14,S16,S17 | Sol high | [S18](handoffs/S18.md) | adccdfc | hardening/deploy gate re-verified by integrator |
| S23 | W5 | COMPLETED | S14,S20,S22 | Terra high | [S23](handoffs/S23.md) | adccdfc | Android main 3b6f7b1; chat wired into navigation |
| S27 | W5 | PARTIAL | S24,S26 | Sol xhigh | [S27](handoffs/S27.md) | adccdfc | DEFERRED out of PoC scope (ADR-009); merged, disabled by default |
| S28 | W6 | PENDING | S27 | Sol xhigh | — | — | DEFERRED out of PoC scope (ADR-009); not started |
| S30 | W6 | READY | S12,S14,S21,S23 | Sol high | — | — | S21 device gate closed; auth/text E2E |
| S31 | W6 | READY | S16,S25,S26 | Fable 5.1 xhigh | — | — | S26 physical gate passed; voice E2E |
| S32 | W6 | READY | S17,S23,S25 | Sol high | — | — | S23 integrated; tool E2E dependencies complete |
| S33 | W7 | PENDING | S18,S30,S31,S32 | Fable 5.1 xhigh | — | — | release gate; S28 dropped per ADR-009 |

## Current wave gate

W5 is integrated and PASSED as re-scoped. Backend hardening (S18) and Android
text chat (S23) meet their Definition of Done. S27 foreground wake does not meet
its original Definition of Done and is no longer required to: wake-phrase
activation is out of internal-beta scope per ADR-009.

Backend is at coordination main with the full gate re-run by the integrator:
ruff/format, mypy, 142 passed / 1 credential skip against PostgreSQL 16 and
Redis 7, `alembic check` plus migration up/down/up, and contract validation
(5 schemas, 18 operations, 36 fixtures, 25 error codes). Container smoke,
release-step migration on empty and existing databases, readiness fail-closed
behaviour, backup/restore drill and dependency scan were verified inside S18.
Deployment now assumes exactly one backend instance (OI-S18-002).

Android main is `3b6f7b1`: chat and conversation list are reachable and
account-scoped, wake-word lifecycle is wired behind a single microphone
arbiter, sign-out is reachable from Settings, and the gate passed with 68 unit
tests plus mock/live debug assembly and lint.

S21-R2 then closed the real Google auth gate on a physical SM-S938B against a
configured live build: sign-in, session restore, refresh, logout and re-login
all pass at backend `b7a6c10`. That gate exposed and fixed a backend defect —
the certificate-fetch wrapper rejected google-auth's positional transport call,
so every genuine ID token answered 401; see [S21-R2](handoffs/S21-R2.md). The
same device run also exercised the W5 text chat and the Settings wake controls.
Wake quality, latency, CPU and battery remain unmeasured by design.

S30, S31 and S32 are READY, and they are the only remaining inputs to the S33
release gate. S27 and S28 are deferred out of scope (ADR-009): the internal beta
activates voice by tap-to-talk only.

## Blocking conditions

None. Wake-phrase activation is no longer a blocker: S27 and S28 are out of
internal-beta scope by product decision ([ADR-009](../adr/0009-defer-wake-word-activation.md)).
The wake module stays merged and disabled by default, and no quality, latency,
CPU or battery figure is claimed anywhere. If activation returns to scope, S27
resumes from its handoff and needs an independent speech corpus plus an
eight-hour visible-app battery run before S28 may start.

## Integration history

| Wave | Integrated at | Commit | Gate result | Integrator |
| --- | --- | --- | --- | --- |
| W0 | 2026-09-10 | b56c17f | PASS | S00 bootstrap |
| W1 | 2026-09-10 | 065617a | PASS | parallel S10/S20 + wave integration |
| W2 | 2026-09-11 | 3b7e8eb | PASS | parallel W2 + S24 physical-device closeout |
| W3 | 2026-09-12 | c86b0ae | PASS | parallel W3 + cross-repository wave integration |
| W4 | 2026-09-14 | e9ca3f1 / Android 7db3e6f | PARTIAL | parallel W4 + S26 physical closeout; S21 OAuth gate pending |
| W5 | 2026-09-16 | adccdfc / Android 3b6f7b1 | PASS | parallel S18 + Android S23/S27 integration; S21-R2 auth closeout at b7a6c10; wake deferred by ADR-009 |
