# ADR-0012: Memory Ledger and privacy V1

- Status: Accepted
- Date: 2026-10-09
- Sources: [Review](../Mia_Architecture_Review.md) §0, §7.1–§7.5, §11 («Память»)
- Extends: ADR-007 (ephemeral raw audio)
- Contracts: `contracts/mvp/v1/memory.schema.json`,
  `contracts/mvp/v1/openapi.yaml` (`/v1/memory/*`),
  `app/core/ports/memory_provider.py`

## Context

The assistant must remember facts and people, and the user must have no
questions about it: see everything, know where it came from, correct or delete
it. With a cloud runtime and BYOK providers the operator cannot be made
technically unable to read data during a request, and providers keep requests
under their own retention. Options V1–V4 are compared in Review §7.2.

## Decision

1. **V1 now, data model ready for V2.** Memory is a **ledger** of records:
   - `Fact {id, text, category, source, status, sensitivity, scope,
     created_at, updated_at, last_used_at}`;
   - `Person {id, display_name, aliases, relation, identifiers, trusted,
     source, status, sensitivity, …}`;
   - `Preference {id, key, value, source, …}`.
2. **Writes need consent.** `memory.remember` (model) and `memory.suggest`
   (apps) create `suggested` records; one tap makes them `active`. Auto-save
   may be enabled per non-sensitive category. Deletion is a hard delete.
3. **People are referenced, not exposed.** The model sees `person:<id>`,
   display name and relation; identifiers (Telegram id, phone, email) are
   substituted by the core at execution time and never reach the model, logs
   or metrics. Phone contacts are read per run through a device tool and are
   not stored unless the user says «запомни».
4. **Encryption at rest with a per-user DEK** wrapped by a master key. Deleting
   the DEK crypto-shreds the user's data, including backups. V2 later moves the
   wrapping to a user key (passkey PRF + recovery code) without changing the
   records; `scope: "automation"` marks facts background jobs may read.
5. **Minimal context:** only selected `active` facts go to the provider (FTS
   first, embeddings only if FTS proves insufficient); the UI shows what the
   current model sees and its provider's retention.
6. Incognito turns write neither history nor memory; history retention is
   30 days / 1 year / forever / none. Raw audio never leaves the phone.
7. Apps get memory only through scopes (`memory.context` by default,
   `memory.read:<category>` logged per read, `memory.suggest`); what an app
   already read cannot be recalled.
8. Export as `/memories/*.md`; import from other assistants goes through the
   suggestion queue.

## Consequences

- M17 owns storage, encryption, FTS and the «Что Mia знает» API/UI; M50 owns
  app scopes and the access log.
- The initial category set (`profile`, `work`, `home`, `health`, `finance`,
  `interests`, `other`) is extended additively; scope names follow it.
- The operator can technically decrypt memory while the key is on the server;
  the privacy page says so honestly.
