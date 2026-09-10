# MIA session launcher

Это короткая точка входа в план реализации. Подробный план, архитектура сессий, зависимости, write scopes, acceptance criteria и model policy находятся в [docs/implementation/SESSION_EXECUTION_PLAN.md](docs/implementation/SESSION_EXECUTION_PLAN.md).

Codex получает этот workflow через `AGENTS.md`, а Claude Code — через `CLAUDE.md`; поэтому синтаксис команды одинаков для Sol/Terra и Fable.

Модель выбирается при открытии новой Codex/Claude session: текстовая команда сама не переключает провайдера. Используйте `Preferred executor` из model table; если он недоступен, указанный `Fallback` может выполнить session без дополнительного согласования и записывает фактическую модель в handoff.

## Команды пользователя

Достаточно одной строки:

~~~text
Выполни сессию S10 из session_plan.
~~~

Допустимы сокращения:

~~~text
Выполни S10.
Проверь готовность S16.
Продолжи S26 по handoff.
Выполни все готовые сессии wave W2 параллельно по session_plan.
Интегрируй завершённые сессии wave W3.
~~~

Пользователь не указывает SHA коммитов, пути репозиториев, branch/worktree, список handoff-файлов, open issues, тестовые команды или длинный prompt. Это механический контекст, который исполнитель обязан восстановить из repository state и coordination files.

## Что делает исполнитель автоматически

1. Находит session card по ID и читает только общий контракт, эту card и необходимые архитектурные разделы.
2. Проверяет `STATUS.md`, прямые dependency handoffs и фактическое состояние backend/Android repositories.
3. Сам определяет base/head commits, нужную branch/worktree, актуальные контракты и релевантные open issues.
4. Проверяет готовность зависимостей и пересечения write scope.
5. Выполняет реализацию и тесты в пределах session card.
6. Создаёт `docs/implementation/handoffs/Sxx.md` и обновляет выделенный session block в `OPEN_ISSUES.md`.
7. Возвращает краткий результат, verification и следующую допустимую команду.

Если сессия ещё не готова, исполнитель не просит заполнить prompt и не угадывает недостающие коммиты. Он называет конкретную незавершённую зависимость и предлагает короткую следующую команду, например `Выполни S00`.

Рекомендуемая рабочая директория для всех orchestration-команд — `/Users/aleksey/PycharmProjects/mia-helper`. После создания Android repository сессия S20 добавляет туда компактные `AGENTS.md`/`CLAUDE.md` forwarders, чтобы тот же вызов работал и из Android checkout.

## Coordination files

- Детальный план: [docs/implementation/SESSION_EXECUTION_PLAN.md](docs/implementation/SESSION_EXECUTION_PLAN.md)
- Архитектура: [docs/POC_ARCHITECTURE_AND_IMPLEMENTATION_PLAN.md](docs/POC_ARCHITECTURE_AND_IMPLEMENTATION_PLAN.md)
- Статусы: [docs/implementation/STATUS.md](docs/implementation/STATUS.md)
- Неблокирующие вопросы: [docs/implementation/OPEN_ISSUES.md](docs/implementation/OPEN_ISSUES.md)
- Внутренний context-packet template: [docs/implementation/SESSION_PROMPT_TEMPLATE.md](docs/implementation/SESSION_PROMPT_TEMPLATE.md)
- Handoffs: [docs/implementation/handoffs](docs/implementation/handoffs)

## Secret boundary

`.env` существует, но никогда не читается и не выводится исполнителем. Session может опираться только на задокументированные имена переменных и результат безопасного запуска приложения; значения секретов не попадают в context packet или handoff.
