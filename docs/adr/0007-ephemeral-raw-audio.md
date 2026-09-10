# ADR-007: Keep raw audio ephemeral

- Status: Accepted
- Date: 2026-09-10

## Context

Raw voice recordings carry greater privacy and breach impact than the
transcripts needed for conversation history.

## Decision

Raw PCM exists only in bounded in-memory buffers during an active session and is
not persisted or logged by default. Canonical history stores transcripts,
delivery metadata, and aggregate usage/latency measurements.

## Consequences

Audio debugging must use explicit synthetic fixtures or a separately authorized,
consented diagnostic flow. Crash reporting, metrics, and provider-error logging
must exclude frame contents.
