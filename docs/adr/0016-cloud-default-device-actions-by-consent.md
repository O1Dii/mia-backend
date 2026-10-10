# ADR-0016: Cloud by default; device actions and device data only on request, with consent on the device

- Status: Accepted
- Date: 2026-10-10
- Decided by: the user (discussion after live checks on https://mindmapgen.xyz/mia/)
- Sources: [Mia_Architecture.md](../Mia_Architecture.md) §5–6 (Personal Node, `waiting_for_node`),
  [Mia_Backlog.md](../Mia_Backlog.md) §2; handoffs M13 (router, approvals, device tool channel),
  M16 (jobs, push), M17 (Memory Ledger, access log), M23 (device tools), M24 (WebView hand-off)
- Related: ADR-0010 (MCP-native plugins), ADR-0012 (memory and privacy V1)

## Context

Live checks showed that two mechanisms were mixed up:

- **Synchronization** — state of the account (timers, reminders, notes, memory, feed, jobs) lives on the
  server; every client (web, phone, Telegram bot) sees the same state. This already works.
- **Remote execution** — an action or a read on a specific device (phone contacts, the system Clock app,
  opening Telegram with a draft). Today device tools are offered only to a run started on the same device
  (`app/modules/tools/router.py:305-310`); a web run, a WebView page in the app, a job or the bot get none.
  At the same time the `builtin.alarm` skill tells the model to set a system alarm on the phone as well,
  without asking.

The original Personal Node idea (a user-owned node with capabilities, routing of work to it,
`waiting_for_node` with an explicit fallback) is about where the runtime and data live. Its routing model
also fits remote execution on a phone.

## Decision

1. **Cloud by default.** Timers, reminders, «Mia alarms», notes, digests and jobs run in the account
   (server runtime + worker) and reach the user as notifications (inbox, push, Telegram bot). On the phone
   a Mia alarm may look like an alarm (full-screen, sound) — presented by the Mia app, not the system Clock.
2. **The device is an explicit target.** The model acts in a device's apps (system Clock, Telegram, opening
   apps) only when the user asked for it or confirmed Mia's offer. Skills and prompts must not trigger
   device actions implicitly. After the first cloud alarm Mia may offer once «могу ещё поставить в
   «Часах» на телефоне» and remembers the answer as a preference.
3. **Requester vs target.** A request from the target device itself (native chat/voice, **and the app's
   WebView on that phone**, whose web session is bound to the device by the M24 hand-off) needs only the OS
   permission and the capability being enabled in Mia's settings on that device. A request from
   **another client** (web on a computer, the Telegram bot, a job, another device) needs consent on the
   target device.
4. **Consent is given only on the target device.** The phone shows «<client> просит: <capability> для
   запроса «…»» via a high-priority push with actions and in the app. Scopes:
   - **this request** (default): valid until the run ends, covers repeated calls of that capability;
   - **persistent**: chosen in the phone's settings, bound to the specific requester (this browser / this
     device), expires after 30 days for other clients, removed when that session signs out, revocable.
5. **Reads and actions differ.** Reads of device data (contacts) follow §4. Device actions that only affect
   the user (system alarm, open app) may be pre-allowed persistently. Actions with external effect
   (Telegram draft to a person) keep the per-action approval of ADR-0010/M13 on the device.
6. **Data minimization.** A device read returns the minimum: matched names and counts, never phone numbers
   or other contact fields to the cloud; the device resolves numbers locally for hand-offs. The consent
   text says what the model will see.
7. **Memory first.** People confirmed in Memory are resolved without touching the device; device contacts
   are the second step, under §3–§4.
8. **No silent fallback.** A cross-client request waits for the device for a bounded time
   (`waiting_for_device`, default 2 min), shows «Подтвердите на телефоне», and fails with a clear message if
   the device is offline, locked or declines. It never silently degrades to another device or the cloud.
9. **Plugins declare where they work.** `mia.json` declares cloud tools and device capabilities with a
   user-facing description; the store shows a «Где работает» block (cloud / on the phone on request /
   in an app). Device capabilities used by a plugin are listed on its consent screen.
10. **Audit.** The phone's settings list device reads/actions requested by other clients: what, from where,
    when, decision (extends the M17 access log).
11. **One policy for all nodes.** The decision is `allow | ask | deny` over (capability, requester, target,
    scope); `ask` is an approval that can be resolved only on the target device. The phone is the first node;
    a future Personal Node (PC, speaker) joins with the same protocol.

## Consequences

- `builtin.alarm` stops setting the system alarm implicitly (skill text and router), and device tools become
  reachable from other clients only through the consent flow.
- New work (sessions to be planned): backend policy + `waiting_for_device` + actionable push + device-bound
  web sessions for the WebView; Android consent sheet, settings (capabilities, persistent grants, audit),
  Mia alarm presentation; manifest «Где работает» + store block + skill updates.
- Persistent grants and their expiry must be visible and revocable; a stolen web session cannot read device
  data without a consent on the phone.
- Personal Node stays in the backlog; this ADR fixes the routing/consent model it will reuse.
