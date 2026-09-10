# ADR-004: Keep text and voice in one conversation history

- Status: Accepted
- Date: 2026-09-10

## Context

Users must continue a voice turn in text and recover completed history after a
reconnect or provider change.

## Decision

Text and voice are modalities of one Conversation/Turn/Entry aggregate.
PostgreSQL is canonical; provider session history is an optimization only.
Entries distinguish role, kind, modality, sequence, and delivery state.

## Consequences

All clients see a coherent timeline and idempotency is enforced at the turn
boundary. Transcript accuracy and delivery metadata remain explicit instead of
presenting provider transcription as guaranteed authored text.
