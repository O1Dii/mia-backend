# MIA internal session context template

Этот файл заполняет исполнитель автоматически после короткой команды пользователя. Пользователь не копирует шаблон и не указывает commits, paths или handoffs вручную.

Минимальная команда пользователя:

~~~text
Выполни сессию <SESSION_ID> из session_plan.
~~~

## Dispatcher-generated context packet

Перед реализацией исполнитель внутренне формирует следующий packet из актуального состояния репозиториев. Его не требуется сохранять отдельным файлом, если session card не требует этого.

~~~text
Session: <SESSION_ID — resolved from the user command>
Title/outcome: <resolved from the session card>
Readiness: <resolved from STATUS, dependency handoffs, and repository state>

Repositories:
- coordination/backend: <auto-discovered absolute path>
- Android: <auto-discovered absolute path or N/A>

Resolved bases:
- backend base/head: <git-derived SHA or N/A before git initialization>
- Android base/head: <git-derived SHA or N/A>
- coordination base/head: <git-derived SHA or N/A>
- branch/worktree: <auto-selected according to the session plan>

Read in this order:
1. session_plan.md and AGENTS.md.
2. SESSION_EXECUTION_PLAN.md sections 3–9 and 11–14.
3. The requested <SESSION_ID> card only.
4. Handoffs of direct dependencies only.
5. Relevant OPEN_ISSUES entries by affected session/component.
6. Architecture sections and source files named by the card.
7. Additional source files discovered by focused search only when needed.

Write scope: <copied from the card and checked against active sessions>
Out of scope: <copied/inferred from the card>
Acceptance criteria: <copied from the card>
Required verification: <copied from the card and adapted to actual build tooling>
Context ceiling/model: <resolved from the model table>
Stop conditions: <derived from sections 3, 6, 7, and 12>
~~~

## Automatic preflight

The executor must:

1. Confirm that every direct dependency is `COMPLETED` and its handoff/commit is available and consistent with git history.
2. Inspect `git status`, recent `git log`, existing worktrees, and uncommitted user changes without modifying them.
3. Confirm that the write scope does not overlap another active session.
4. Check required tools/configuration without opening `.env` or printing secret values.
5. If the session is not ready, stop before implementation and return the exact unmet dependency plus a short valid next command.

Missing mechanical metadata is never a reason to ask the user to fill this template: the executor discovers it. Ask the user only when a genuine product decision, permission, destructive action, or inaccessible external state blocks progress.

## Automatic closeout

Before finishing, the executor:

1. Reviews the diff and preserves unrelated user changes.
2. Runs the required targeted and relevant aggregate verification.
3. Updates only the session's allocated block in `docs/implementation/OPEN_ISSUES.md`.
4. Creates `docs/implementation/handoffs/<SESSION_ID>.md` from `_TEMPLATE.md`.
5. Records exact commits, contracts, test results, limitations, and downstream starting points.
6. Updates status/integration metadata according to the ownership rules.
7. Reports `COMPLETED`, `PARTIAL`, `BLOCKED`, or `FAILED`.

Expected final response:

~~~text
<SESSION_ID> — <STATUS>

Outcome: <one sentence>
Verification: <short list>
Handoff: <clickable absolute path>
Open issues: <IDs or none>
Next command: <one short command>
~~~
