# ADR-005: Detect the wake word locally

- Status: Accepted
- Date: 2026-09-10

## Context

Continuous microphone upload would violate the intended privacy boundary and
create unnecessary cost and network dependence.

## Decision

Audio before activation remains on the device. Tap-to-talk is implemented
first, foreground keyword spotting follows, and background/system-assistant
capability is evaluated separately. A wake-word trigger reuses the same voice
session protocol and may include a bounded local pre-roll.

## Consequences

The client owns wake-word quality, device capability, and battery evaluation.
The server never assumes that a specific trigger opened a session. The final
wake phrase remains a product decision tracked outside this ADR.
