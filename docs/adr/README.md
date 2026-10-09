# Architecture decision records

ADRs capture decisions that all implementation sessions may rely on. Accepted
records are changed by superseding them with a new ADR rather than silently
rewriting their meaning.

| ADR | Decision | Status |
| --- | --- | --- |
| 001 | Modular monolith for the PoC backend | Accepted |
| 002 | Backend-mediated realtime provider access | Accepted |
| 003 | Provider-neutral normalized AI events | Accepted |
| 004 | Unified text and voice history | Accepted |
| 005 | Local wake-word detection | Accepted |
| 006 | Explicit backend tool loop | Accepted |
| 007 | Ephemeral raw audio | Accepted |
| 008 | Provisional Android application ID | Provisional |
| 009 | Defer wake-phrase and system-assistant activation past the PoC | Accepted |
| 0010 | [MCP-native plugins with a `mia.json` envelope](0010-mcp-native-plugins.md) | Accepted |
| 0011 | [Run profiles and the speech/display response contract](0011-run-profiles-and-response-contract.md) | Accepted |
| 0012 | [Memory Ledger and privacy V1](0012-memory-ledger-and-privacy-v1.md) | Accepted |
| 0013 | [«Войти через Mia»: OAuth 2.1 authorization server for apps](0013-sign-in-with-mia.md) | Accepted |
| 0014 | [One primary product model until the MVP is accepted](0014-single-primary-product-model.md) | Accepted |
| 0015 | [The MVP plan supersedes S28, S31-R1 and S33](0015-supersede-s28-s31r1-s33.md) | Accepted |

ADR-0010…0015 record the MVP architecture (variant E) from
[Mia_Architecture_Review.md](../Mia_Architecture_Review.md); their
machine-readable counterparts are in `contracts/mvp/v1` and `app/core/ports`.
