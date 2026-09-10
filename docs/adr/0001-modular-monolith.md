# ADR-001: Use a modular monolith for the PoC backend

- Status: Accepted
- Date: 2026-09-10

## Context

The PoC needs clear auth, conversation, realtime, orchestration, tool, and usage
boundaries, but does not yet have measured scaling requirements that justify a
distributed system.

## Decision

Deploy one FastAPI backend. Modules expose application/domain ports and keep
provider and persistence adapters behind those ports. API code does not call AI
SDKs or persistence implementations directly.

## Consequences

Local development and transactions remain simple. Module boundaries can support
later extraction, but Kafka, Kubernetes, and independent services are out of
scope until load or ownership data demonstrates a need.
