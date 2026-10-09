@AGENTS.md

## Claude Code session execution

- Use the same one-line commands and dispatcher contract defined in `session_plan.md`.
- When the requested session lists a Claude model (Fable 5.1, Opus 5.5, Sonnet 5.5, Haiku 5.5) as preferred executor, use the effort level from the model table. Do not raise `high` to `xhigh` or `max` unless the table or a bounded repair decision explicitly requires it.
- Treat `SESSION_PROMPT_TEMPLATE.md` as an internal context assembly checklist. Never ask the user to fill it.
