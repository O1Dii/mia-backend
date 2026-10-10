# ADR-0017: Multiple chats with their own context; tasks, notifications and memory stay shared

- Status: Accepted
- Date: 2026-10-10
- Decided by: the user (after live checks on https://mindmapgen.xyz/mia/)
- Supersedes: [Mia_Architecture_Review.md](../Mia_Architecture_Review.md) §5.2(f) «Одна лента вместо
  множества чатов» and the single-feed scope of M18 (`GET /v1/feed`, voice forced to the main feed)
- Related: ADR-0004 (unified conversation aggregate), ADR-0011 (run profiles), ADR-0012 (memory),
  ADR-0016 (cloud by default)

## Context

The MVP shows one feed per user: voice and chat share it, the model sees the recent window plus an
episodic summary. In live use the user wants several conversations with different contexts at the same
time and a fresh dialog per assistant invocation, as in Gemini. The backend already has a conversation
aggregate and runs accept `conversation_id`; voice is forced to the main feed (`runs.py` `voice_feed_only`).

## Decision

1. **Chats.** A user has many chats (conversations). Each chat has its own message history and model
   context (recent window + its own summary). Chats can be created, renamed (auto-title by the model),
   pinned, deleted, and switched between on web and Android.
2. **Shared account state.** Memory (people, facts, preferences), installed plugins and their state,
   jobs, schedules, inbox, notifications and approvals belong to the account, not to a chat. Switching
   chats changes only the conversation context.
3. **Assistant invocation.** The side key (and later the wake word) starts a **new chat**. If the previous
   assistant turn was less than ~5 minutes ago, the screen offers «Продолжить прошлый» to continue that chat.
   Opening the app's chat screen shows the last opened chat.
4. **Task results.** A job started from a chat posts its result card into that chat **and** into the shared
   inbox / push / the «Сейчас» strip. Schedules and plugin-originated events without a chat go only to
   the shared surfaces. Approvals are shown in the originating chat and in the shared approvals list.
5. **Telegram bot.** The conversation with the Mia bot is its own chat «Telegram»; `/new` starts a new one.
6. **Retention.** A chat is deleted 60 days after its last message unless pinned; the period is a user
   setting (30 / 60 / 180 days / never). Deleting chats never deletes Memory entries; facts confirmed from a
   chat stay until the user removes them.
7. **API.** Conversations become a first-class API (list, create, update, delete, pin; feed per
   conversation). `GET /v1/feed` without a conversation keeps working for compatibility (latest chat).
   Voice runs may target any chat.

## Consequences

- Backend: conversations API, per-chat feed and summary, retention job, job → chat result cards, voice
  without the forced main feed, Telegram chat, «continue within 5 minutes» support.
- Web and Android: chat list, new chat, switching; the assistant invocation flow creates a chat.
- Contracts: additive changes in `contracts/mvp/v1` (conversations paths and schemas).
- M60 acceptance covers multiple chats instead of the single feed.
