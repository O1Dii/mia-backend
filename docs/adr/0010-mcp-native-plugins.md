# ADR-0010: MCP-native plugins with a `mia.json` envelope

- Status: Accepted
- Date: 2026-10-09
- Sources: [Review](../Mia_Architecture_Review.md) §0, §5.4.1–§5.4.5, §11
  («Формат плагина», «Магазин»)
- Contracts: `contracts/mvp/v1/plugin-manifest.schema.json`,
  `contracts/mvp/v1/device-tools.schema.json`, `app/core/ports/tool_router.py`,
  `app/core/ports/device_tool_channel.py`

## Context

The PoC has its own plugin formats (`contracts/plugins/v1` recipes, `v2`
declarative applications) executed by `app/web_poc`. By October 2026 MCP
(spec `2026-07-28`, stateless Streamable HTTP, OAuth 2.1), Agent Skills
(`SKILL.md`) and MCP Apps (`ui://` resources in sandboxed iframes) cover what
those formats tried to do, and official servers exist for the first
integrations (Linear). MCP does not define a store, installation, grants,
approvals, budgets, schedules, inbound events, dashboards or memory; those
stay a Mia host layer (Review §5.4.3).

## Decision

1. A plugin is a **`mia.json` envelope** (`schemaVersion: "mia.plugin/1"`)
   around standard parts: the executable part is an **MCP server**, UI is
   **MCP Apps**, instructions are **`SKILL.md`**. Mia Core is the MCP host
   with one MCP client per connected server.
2. `server.kind` in the MVP is one of:
   - `builtin` — Python MCP server inside the core process (timer, notes,
     focus, the Telegram bot);
   - `remote` — Streamable HTTP MCP server by URL with `auth` `none`,
     `oauth` (Mia stores the user's token per server) or `mia_token`;
   - `declarative` — v2-style `actions` + `view`; Mia itself is the MCP
     server, actions become tools and the view becomes an MCP App.
   `worker` (T2 isolated agent-worker, Review §4) is reserved and is added
   additively after the MVP.
3. **Device tools** are not plugins: the Android client registers JSON Schema
   tools (`device.*`, `telegram.*`) per device; the core sends calls through
   the run event stream (`device_tool.call`) and receives results by REST.
   A manifest may list the device tools it relies on in `requires.deviceTools`.
4. `tools` in `mia.json` is a **policy overlay** on the server's `tools/list`:
   every tool gets `risk` (`read` / `write_reversible` / `write_external`),
   optional `voice`, `timeoutSeconds`, `task`. Publication checks that the
   overlay equals the server's tool set and records a digest of tool
   descriptions; a changed digest or wider permissions require new consent.
5. Tiles are an **Mia extension** over MCP Apps: `surfaces.tiles[]` with
   `resourceUri` under `ui://<plugin id>/`, optional `bundled` HTML (own UI on
   top of a third-party server), sizes, mobile mode and a `refresh` that calls
   a `read` tool through the host without the LLM.
6. `permissions` declares network allow-list (also the SSRF allow-list; a
   remote server's host must be in it), LLM-via-host with a monthly budget,
   memory access (`context`, `suggest`, `read:<category>`), notifications and
   schedules.
7. Tools return two views: `content` (compact summary for the model) and
   `structuredContent` / MCP App (full data for the screen).
8. Store mechanics in the MVP: only the team publishes; «add your own MCP by
   URL» installs a remote server into the user's own account only.

## Consequences

- `contracts/plugins/v1|v2` stay for the PoC; the v2 builder now emits
  `mia.json` with `server.kind: "declarative"` (M14/M15).
- Examples `examples/plugin-manifest/{timer,linear,mindweaver,focus}.mia.json`
  cover every MVP server kind and are validated with cross-field rules
  (remote host in `network`, tile refresh uses a declared `read` tool,
  declarative actions equal tools).
- Executors, signed releases, review and paid plugins stay out of the MVP
  ([Mia_Backlog.md](../Mia_Backlog.md)).
- Fewer than ~10 plugins at start: all tool definitions go to the model,
  filtered by run profile; tool search is deferred.
