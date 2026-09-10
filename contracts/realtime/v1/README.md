# MIA realtime protocol v1

The REST API creates a short-lived realtime session. The client then opens the
returned authenticated WebSocket and sends `session.configure` as its first text
frame. The server must reply with `session.ready` before accepting audio.

## Frame types

- Text frames are UTF-8 JSON and validate against the client/server schemas.
- Client binary frames are raw input PCM for the currently open
  `input_audio.start` stream.
- Server binary frames are raw output PCM for the currently open
  `response.audio.started` segment.
- Binary frame size must not exceed the negotiated
  `max_binary_frame_bytes`. Input should normally use 20–40 ms chunks.
- A binary frame has no JSON header and inherits the active stream/segment ID.
  WebSocket ordering is authoritative within that segment.

Only one client input stream and one server output segment may be open per
direction at a time. `input_audio.commit` closes the input stream;
`response.audio.completed`, `response.interrupted`, or `error` closes the output
segment. Binary data without an open matching segment is a
`realtime_invalid_event` protocol error.

## Ordering and interruption

Client and server maintain independent monotonically increasing `seq` values,
starting at 1 for each new connection. A reconnect starts new connection
sequences and does not resurrect an unfinished turn. Completed turns come from
canonical storage; unfinished turns become interrupted.

On barge-in, Android clears playback immediately and sends `response.cancel`.
After cancellation, it ignores binary output associated with the cancelled
`response_id`. The server emits `response.interrupted` with delivered duration
metadata and must not open another segment for that response.

## Compatibility

Events reject unknown fields to expose drift early. A client advertises optional
v1 capabilities in `session.configure`; an implementation must not emit behavior
requiring a capability that was not negotiated. Breaking envelope, event, or
binary-frame changes require protocol v2.

The exhaustive fixtures under `fixtures/` define one valid example for every v1
control event kind.
