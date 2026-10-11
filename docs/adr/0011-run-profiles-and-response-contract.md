# ADR-0011: Run profiles and the speech/display response contract

- Status: Accepted
- Date: 2026-10-09
- Sources: [Review](../Mia_Architecture_Review.md) §0 (item 4), §5.1, §5.2(a)–(f),
  §5.3, §11 («Harness»)
- Extends: ADR-003 (provider-neutral events), ADR-006 (explicit tool loop)
- Contracts: `contracts/mvp/v1/run-events.schema.json`,
  `contracts/mvp/v1/assistant-response.schema.json`,
  `contracts/mvp/v1/approval.schema.json`, `contracts/mvp/v1/job.schema.json`,
  `contracts/mvp/v1/openapi.yaml` (`/v1/runs*`), `app/core/ports/*`

## Context

A personal assistant answers by voice on a phone, in web chat, in the
background and on behalf of third-party apps. Voice needs a first sound within
~6 s and must not read out markdown, links or ids, while the screen should
still show everything. Long work must not hold a voice turn open.

## Decision

1. Every request is a **run** with a profile:

   | Profile | Initial limits | Notes |
   | --- | --- | --- |
   | `voice` | ≤ 3 model steps, ≤ 2 tool calls (parallel reads of one step count as one, see Amendment 2026-10-11), ~6 s to first sound | only tools marked `voice`; writes need on-screen approval; long work → `background.start` |
   | `chat` | ≤ 12 steps, 60–150 s | full tool set, cards, approvals |
   | `job` | own budget, ≤ 30 min | no user on the line; result → inbox + notification |
   | `app` | budget and scopes of the app grant | only tools covered by `tools.use:<plugin>` |

   Limits are run-engine configuration (M12), not wire contract.
2. The final answer is `{speech, display, followups}`: `speech` is 1–2 plain
   sentences (target ≤ 250, hard limit 300 characters, no markdown, links,
   line breaks); `display` is `null` or one of `text` / `card` / `app`
   (`ui://` MCP App) / `link`; `followups` ≤ 3. Models without structured
   output get their text split by the run engine.
3. Runs stream **run events** over SSE (`GET /v1/runs/{id}/events`): envelope
   `{event_id, run_id, sequence, cursor, type, at, payload}`; `sequence`
   starts at 1 and grows by exactly 1; `cursor` is opaque, equals the SSE id
   and resumes via `Last-Event-ID` or `?cursor=`. Types: `run.started`,
   `message.delta`, `tool.call.started`, `tool.call.completed`,
   `approval.requested`, `approval.resolved`, `device_tool.call`,
   `memory.suggested`, `job.created`, `response.final`, and terminal
   `run.completed` / `run.failed` / `run.cancelled`.
4. Tool calls go through the **ToolRouter** port. `write_external` always
   creates an **Approval** with exact arguments, `args_hash` (canonical JSON
   SHA-256) and `expires_at`; the client echoes `args_hash` in its decision;
   an approval is single-use. A timed-out external write is `uncertain` and
   never retried automatically. Tool output and message text are untrusted
   data.
5. The core always offers `background.start/status/cancel`,
   `schedule.create`, `memory.remember` and `notify`. Finished jobs land in
   the **inbox**; the next voice run mentions unread items.
6. One continuous feed per user; voice always writes to it.
7. Clients are thin and use the same public API as third-party apps.

## Consequences

- M12 implements the engine against `ModelGateway`, `ToolRouter`,
  `MemoryProvider`, `ApprovalService` and `DeviceToolChannel` and can test
  against `tests/fakes` before M11/M13/M17 land.
- The old `contracts/openapi/v1` conversation SSE and `contracts/realtime/v1`
  stay for the PoC paths until clients move (M22, M30).
- Splitting text for models without structured output must be verified on the
  primary model (ADR-0014).

## Amendment 2026-10-11: parallel reads count as one voice tool call

Reason: the acceptance run M60 (OI-M60-001) showed that the limit of 2 tool calls cuts the voice question
«Что у меня сегодня?» to two of its three sources (Linear, notes, timer); the model asks for all of them in
one step. User decision of 2026-10-11 (option b).

- In the `voice` profile, `read`-risk calls issued together in **one model step** share **one** unit of the tool
  budget, up to 4 reads per step (`RUN_VOICE_MAX_PARALLEL_READS`, default 4). A further read of that step is
  not executed: the model gets `{"status": "skipped", "error": "parallel_read_limit"}`.
- Calls of any other risk (`write_reversible`, `write_external`) count individually, as before. The budget
  stays 2 units (`RUN_VOICE_MAX_TOOL_CALLS`) and the run stays at ≤ 3 steps, so a turn is still at most
  one tool round plus one more unit; the ~6 s to first sound is unchanged as a target (to be measured
  live, UC-M60-8).
- The risk is the one the ToolRouter reports in the tool descriptor (`read`); nothing new is classified.
- `run.completed.tool_calls` reports the calls actually executed. `chat`, `job` and `app` are unchanged
  (`max_parallel_reads = 1`: every call counts on its own).
