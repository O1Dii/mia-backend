# MIA REST API v1

`openapi.json` is the source of truth for REST and text-stream endpoint shapes.
The WebSocket appears only as an extension because OpenAPI does not model its
bidirectional frames; use `contracts/realtime/v1` for that protocol.

All state-changing endpoints require `Idempotency-Key`. Every response carries
`X-Request-ID`; error bodies additionally carry the same value as `request_id`.
Pagination cursors are opaque and must not be interpreted by clients.

The SSE response from `messages:stream` uses named events documented by the
`x-sse-event-types` extension. S14 may add an additive schema for their JSON
`data` payloads before implementation, but must preserve these names or propose
a reviewed versioned contract change.

Each SSE `data` field is a `ConversationStreamEvent` selected through the
`x-sse-data-schema` extension. The envelope is versioned with `v: 1`; retries
of an already completed idempotent turn emit only `message.completed` with
`payload.replayed: true`, and clients then reconcile canonical entries through
the history endpoint.
