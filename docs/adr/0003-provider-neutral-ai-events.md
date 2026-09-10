# ADR-003: Normalize provider output as AIEvent

- Status: Accepted
- Date: 2026-09-10

## Context

Text, realtime, conversation, and tool orchestration need one event vocabulary.
Importing Google SDK types outside an adapter would make provider replacement a
cross-cutting client and domain migration.

## Decision

Providers emit the versioned event kinds in
`contracts/ai/v1/ai-event.schema.json`. Provider-specific payloads and model IDs
remain inside provider adapters. Public REST/realtime translation consumes only
normalized AIEvent values.

## Consequences

Fake and Gemini providers are contract-compatible and a future local
STT/LLM/TTS chain can implement the same port. Adding or changing an event kind
requires schema and fixture updates plus consumer review.
