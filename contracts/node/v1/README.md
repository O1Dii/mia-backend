# MIA Personal Node contracts v1 (DRAFT)

Draft by M85 for [ADR-0019](../../../docs/adr/0019-personal-node.md) (Status: Proposed). Nothing implements it yet.
M86 owns it from its first commit. Until M86 merges, changes are additive only. After that they follow the usual
rule: additive changes, or a new version directory for a breaking change. Validate with
`.venv/bin/python contracts/scripts/validate_contracts.py`. The validator's `validate_node()` checks:

- `$id`s;
- examples, in the mvp/v1 layout;
- the tool-call `args_hash`;
- the session fixtures (sequence numbers, acks, stream chunk counts).

| File | What it defines | Implemented by |
| --- | --- | --- |
| `common.schema.json` | ids, node host label, Ed25519 key/signature/nonce encodings, fingerprint, SPKI pin, `dataMode`, capability ids, `node.*` tool names, node error codes | — |
| `pairing.schema.json` | QR/code pairing: start (node), claim (signed-in client), poll (node), `NodeSummary` | M86 |
| `envelope.schema.json` | control-channel frames: handshake (`challenge`/`hello`/`welcome`), `ping`/`pong`/`ack`, `manifest`, `rpc.*`, `revoked`, `close` | M86 |
| `rpc.schema.json` | RPC methods with params and results. Node tools (`node.files.*`, `node.apps.open`, `node.links.open`) are M87. Models (`model.list`/`warmup`/`generate`, streaming) are M88. Tunnel and certificates (`relay.grant`, `cert.issue`) are M91. Push and OAuth forwarding (`push.send`, `oauth.callback`) are M89/M90. Key rotation and unpairing are M86 | M86–M91 |
| `capability-manifest.schema.json` | `CapabilityManifest` (what the node offers; labels only), `NodeDeviceView` (proposed row of the M84 «Устройства» list) | M86 (+M87/M88 capabilities) |

## Transports

1. **Control channel.** One outbound WSS connection from the node to the API:
   `wss://<api host>/<base>/v1/nodes/connect`. It carries JSON text frames (`envelope.schema.json`) and goes
   through the normal TLS of the Mia API. The cloud is a legitimate peer here: it runs the cloud runtime that calls node
   tools and node models.
2. **Tunnel (M91).** The node opens a second WSS connection to the relay, `RelayGrant.tunnel_url`. Clients reach
   the node at `https://<host>.<node zone>`. The relay reads only the TLS SNI and forwards the raw TLS bytes over
   the tunnel. TLS 1.3 ends **on the node** with the node's own Let's Encrypt certificate (`cert.issue`), so
   the relay sees ciphertext only. Binary tunnel frames: `u32 stream_id | u8 type | payload`, big-endian. The types
   are `1 OPEN` (no payload), `2 DATA` (≤ 16 KiB), `3 CLOSE` (u8 reason), `4 WINDOW` (u32 credit bytes) and
   `5 PING`. The node opens no listening port on the router.

## Signing strings

Every signature is Ed25519 with the node's device key. It is computed over UTF-8 text joined with `\n`, with no
trailing newline:

| Use | String |
| --- | --- |
| pairing start | `mia-node/1`, `pairing.start`, `<public_key>`, `<signed_at>` |
| pairing poll | `mia-node/1`, `pairing.poll`, `<pairing_id>`, `<signed_at>` |
| hello | `mia-node/1`, `hello`, `<node_id>`, `<connection_id>`, `<nonce>`, `<resume.session_id or ->`, `<resume.last_received_seq or 0>` |
| key rotation | `mia-node/1`, `rotate`, `<node_id>`, `<new_public_key>`, `<signed_at>`, signed by the old **and** the new key |
| manifest | canonical JSON of the manifest without `signature`, using the same canonicalisation as `args_hash` |

Domain separation (`mia-node/1` plus the purpose) stops a signature for one purpose being replayed as another.
The examples are signed with a fixed example key derived from `sha256("mia-node example key - not secret")`.

## Sequence numbers and replay protection

- **Handshake.** Handshake frames carry `seq: 0`. The cloud's `challenge.nonce` is single use and valid for 30 s.
  The `hello` signature binds it to the connection and the node. A recorded `hello` therefore cannot be replayed.
- **Session sequence.** Every other frame carries a per-direction session `seq`. It starts at 1 and grows by exactly
  1. `ack` is the highest contiguous `seq` received from the peer. A frame with `seq ≤ last received` is a duplicate
  after a resume: the receiver drops it and acks again. A gap is a protocol error: close `4400`, reconnect, then resume.
- **Resume.** `hello.resume` sends `{session_id, last_received_seq}` and `welcome` answers with its own
  `last_received_seq`. Each side then retransmits its unacked frames. Each side buffers at most 256 frames or 60 s.
  If a side cannot resume, `welcome.resumed=false` starts a new session at seq 1 and every in-flight call fails.
  For risk `read` the call is `failed`. For actions it is `uncertain`, which ADR-0011 says is never retried
  automatically.
- **RPC dedupe.** Each RPC `id` is the idempotency key. The node keeps executed `tool.call` ids for 24 h in
  persistent storage, so a duplicate returns the cached response (`cached: true`) and the action never runs twice.
  Requests past `deadline_at` are rejected with `expired`. The node estimates clock skew from
  `challenge.server_time` and allows 30 s.
- **`tool.call` integrity.** Before executing, the node recomputes `args_hash`. For an action, the node also
  requires `authorization.basis` to be `approval` or a grant.

## Proposed cloud endpoints and public error codes (added to `mvp/v1` by M86+)

M84 owns `mvp/v1/openapi.yaml` while MW6b runs in parallel, so these endpoints are listed here rather than added
there:

- `POST /v1/nodes/pairings`: no auth; rate limited per IP; `PairingStartRequest` → `PairingStartResponse`
- `POST /v1/nodes/pairings/claim`: bearer or cookie, plus `Idempotency-Key`; `PairingClaimRequest` → `PairingClaimResponse`
- `POST /v1/nodes/pairings/{pairing_id}/poll`: `PairingPollRequest` → `PairingPollResponse`
- `GET /v1/nodes/connect`: WebSocket upgrade for the control channel
- `GET /v1/nodes`, `GET /v1/nodes/{id}`, `PATCH /v1/nodes/{id}` (`name`, `approver`), `DELETE /v1/nodes/{id}`
  (revoke). Nodes also appear in M84's `GET /v1/devices` with `kind: node` (`NodeDeviceView`).
- Error codes:
  - `pairing_code_invalid` (404)
  - `pairing_expired` (410)
  - `pairing_fingerprint_mismatch` (409)
  - `pairing_rate_limited` (429, retryable)
  - `node_not_found` (404)
  - `node_revoked` (410)
  - `node_offline` (event-only, retryable): the run tells the user plainly
  - `node_capability_disabled` (event-only)
  - `node_busy` (event-only, retryable)
  - `node_data_unavailable` (503, retryable): the account's data home is off
