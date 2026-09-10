# ADR-002: Mediate realtime AI sessions through the backend

- Status: Accepted
- Date: 2026-09-10

## Context

Direct Android-to-provider access could remove one network hop, but would expose
provider coupling and weaken central history, quotas, tool policy, and audit.

## Decision

Android opens a provider-neutral WebSocket to MIA. The backend authenticates the
user, manages the provider session, relays bounded audio, executes tools, records
transcripts and usage, and translates provider failures.

## Consequences

Provider credentials never enter the APK and providers can be replaced without
changing the client protocol. The backend must enforce bounded queues and
measure the latency added by the relay. Direct provider access may be evaluated
later only behind an explicit replacement ADR.
