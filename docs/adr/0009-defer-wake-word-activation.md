# ADR-009: Defer wake-phrase and system-assistant activation past the PoC

- Status: Accepted
- Date: 2026-09-16

## Context

S27 implemented a foreground-only, on-device wake phrase («Привет, Мия») with
local enrollment, a bounded pre-roll and no pre-trigger network path. It is
functionally complete but unmeasured: the provisional detection threshold is
supported only by synthetic tone tests, and no false-accept/reject rate, trigger
latency, CPU, thermal or battery figure exists.

Closing those gates needs an independent speech corpus — held-out positive clips
from several voices plus hours of negative conversation, music and TV audio —
and an eight-hour visible-app battery run on a physical device. S28, the system
assistant/hotword spike, must not start from unmeasured detector quality.

Foreground-only activation also carries little user value on its own: while the
app is already open on screen, tapping is faster than speaking a trigger. The
value lives in S28, which is the larger and riskier piece.

The PoC's voice path does not depend on either: tap-to-talk opens the same
session protocol (see [ADR-005](0005-local-wake-word.md)).

## Decision

Wake-phrase activation (S27) and system-assistant activation (S28) are out of
scope for the internal beta. S27 stays PARTIAL with its code merged, disabled by
default and reachable only from Settings. S28 is not started. S33 no longer
depends on S28 and must publish the exclusion as a known limitation rather than
as an unfinished gate.

The internal beta activates voice by tap-to-talk only. No wake-phrase or
background-listening capability is claimed anywhere in product material.

## Consequences

The merged `core/wake` module and its evaluation harness remain available and
cost nothing while disabled; the corpus work is postponed, not discarded.
Whoever resumes this decides first whether to measure and tune the in-house
spectral-template detector or replace it with an established KWS engine, which
would add an external dependency and licence considerations.

OI-ARCH-003, the final wake phrase, stops being a PoC-blocking question and is
revisited only when activation returns to scope.
