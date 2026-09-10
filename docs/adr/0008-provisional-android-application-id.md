# ADR-008: Use a provisional Android application ID

- Status: Provisional
- Date: 2026-09-10

## Context

S20 needs a stable namespace before the production domain, signing identity, and
OAuth clients are finalized. Changing `applicationId` after publishing creates a
different Android application.

## Decision

Use `by.mia.assistant` as both Gradle `namespace` and `applicationId` for local
and internal PoC builds. It is an implementation assumption, not evidence of
domain ownership or approval for public release.

## Consequences

S20 can create deterministic modules and test packages. Before release OAuth
configuration or publication, the owner must resolve OI-ARCH-001 and either
ratify this identifier or supersede this ADR while migration is still cheap.
