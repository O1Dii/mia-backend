# Normalized AIEvent v1

`AIEvent` is the only provider-to-orchestration event vocabulary. Provider SDK
types, model IDs, raw error payloads, and credentials remain inside adapters.

The JSON Schema is a serializable projection used for contract tests and
fixtures. Runtime implementations may carry `audio.delta` bytes as an immutable
byte buffer instead of base64, but must preserve the encoding, sample rate, and
channel metadata. They must not log or persist those bytes.

Event `seq` is monotonically increasing within one provider response/session
stream. `event_id` supports diagnostics and deduplication; it is not a public
resource identifier. Provider errors use the normalized internal codes in the
schema and are translated to the public registry under `contracts/errors/v1`.
