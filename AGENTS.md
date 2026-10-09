# MIA repository instructions

## Secret handling

- Never open, print, search, parse, or copy `.env` or other secret-value files.
- It is safe to use variable names documented in `.env.example`, configuration code, or session cards.
- Secret values never belong in logs, tests, commits, handoffs, `STATUS.md`, or `OPEN_ISSUES.md`.

## Short session commands

When the user writes `Выполни S10`, `Выполни сессию S10 из session_plan`, or an equivalent short command, treat it as a complete implementation request. The user is not expected to provide repository paths, commits, branches, dependencies, prompts, tests, or handoffs.

For such a request:

1. Read `/Users/aleksey/PycharmProjects/mia-helper/session_plan.md`.
2. Read the dispatcher contract in `docs/implementation/SESSION_EXECUTION_PLAN.md` and the requested session card: `Mxx` cards (waves `MWn`) live in `docs/implementation/MVP_SESSION_PLAN.md`, legacy `Sxx` cards in `SESSION_EXECUTION_PLAN.md`; do not preload unrelated cards.
3. Determine readiness from `docs/implementation/STATUS.md`, direct dependency handoffs, and actual repository state.
4. Resolve repository paths, base/head commits, branches, worktrees, relevant open issues, and required tests yourself. Never ask the user to transcribe mechanical metadata that can be discovered locally.
5. Build the internal context packet described by `docs/implementation/SESSION_PROMPT_TEMPLATE.md`; that file is for the executing agent, not for the user to fill in.
6. If dependencies are not complete or verified, do not silently execute prerequisite sessions. Report the exact unmet dependency and the shortest valid command the user can issue next.
7. If ready, implement the whole session, verify it, create its handoff, update its allocated `OPEN_ISSUES.md` block, and update coordination status as specified by the session plan.
8. Ask the user only for a genuine product decision, new authority, inaccessible external state, or a destructive/high-impact action—not for commits, paths, or routine orchestration data.

For `Выполни все готовые сессии wave Wn параллельно` (or `wave MWn` for the MVP plan), execute only dependency-ready sessions with disjoint write scopes. Use isolated branches/worktrees and available parallel agents when the environment supports them. Preserve the integration order and shared-file ownership rules from the detailed plan.

## Sources of truth

- Short launcher and command syntax: `session_plan.md`
- Detailed session cards and dependency graph: `docs/implementation/MVP_SESSION_PLAN.md` (current MVP, `Mxx`) and `docs/implementation/SESSION_EXECUTION_PLAN.md` (contract + legacy `Sxx`)
- Current readiness/integration state: `docs/implementation/STATUS.md`
- Non-blocking questions: `docs/implementation/OPEN_ISSUES.md`
- Session handoffs: `docs/implementation/handoffs/`

