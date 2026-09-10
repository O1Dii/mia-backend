# ADR-006: Execute tools through an explicit backend loop

- Status: Accepted
- Date: 2026-09-10

## Context

Model-proposed function calls may be invalid, unauthorized, duplicated, or
unsafe. SDK automatic execution cannot enforce MIA's audit and confirmation
rules consistently.

## Decision

Every tool call passes through registry lookup, input schema validation,
permission and risk policy, optional user confirmation, timeout-bound execution,
output sanitization, audit persistence, and an explicit result returned to the
model. Mutating tools require idempotency keys.

## Consequences

Unknown or invalid calls fail closed. Read, write, and sensitive risk levels are
visible to policy. The first PoC tool is read-only; write tools cannot bypass
confirmation by changing providers.
