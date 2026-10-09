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
- Прогон на физическом устройстве: [docs/implementation/DEVICE_GATE.md](docs/implementation/DEVICE_GATE.md)
- Handoffs: [docs/implementation/handoffs](docs/implementation/handoffs)

## MVP: M-сессии и волны (актуальный план с 2026-10-09)

Текущий план реализации — [docs/implementation/MVP_SESSION_PLAN.md](docs/implementation/MVP_SESSION_PLAN.md).
Архитектура: [docs/Mia_Architecture_Review.md](docs/Mia_Architecture_Review.md);
хвосты вне MVP: [docs/Mia_Backlog.md](docs/Mia_Backlog.md).

Сессии `Mxx`, волны `MW0…MW7`. Команды те же:

~~~text
Выполни M00.
Выполни все готовые сессии wave MW1 параллельно.
Интегрируй завершённые сессии wave MW1.
~~~

Общий контракт сессии (вход, handoff, OPEN_ISSUES, git, repair) берётся из
`SESSION_EXECUTION_PLAN.md` §3–6, §12–14. Модели, ownership, граф и карточки —
из `MVP_SESSION_PLAN.md`. Для M-сессий используются Claude Opus 5.5 / Sonnet 5.5 /
Haiku 5.5 по таблице §3 этого плана. Старые S28, S31-R1 и S33 заменены
M-сессиями (ADR-0015).

## Новое направление: web-платформа

2026-10-05 пользователь расширил продукт до платформы приложений вокруг локальных
и облачных моделей. Первый localhost POC реализован отдельно от старого voice
release gate: [WEB-P1 handoff](docs/implementation/handoffs/WEB-P1.md).
Обновление по прямому запросу: [WEB-P1-R1](docs/implementation/handoffs/WEB-P1-R1.md) —
исполняемые навыки, общий runtime, дашборд, Интернет, manifest v2 и SDK приложений.
[WEB-P1-R2](docs/implementation/handoffs/WEB-P1-R2.md) — страницы плагинов,
Google-аккаунты/SQLite, браузерный голос и опубликованный amd64 POC
на https://mindmapgen.xyz/mia/ с плиткой общего хаба (2026-10-06).
[WEB-P1-R3](docs/implementation/handoffs/WEB-P1-R3.md) — исправление 403 при
возврате после Google OAuth, узкое исключение для загрузки публичной страницы.

- Новый план и следующие этапы: [WEB_PLATFORM_PLAN](docs/WEB_PLATFORM_PLAN.md).
- Добавление плагинов и будущий SDK: [PLUGIN_DEVELOPMENT](docs/PLUGIN_DEVELOPMENT.md).
- Запуск React/web API: [web/README.md](web/README.md).

WEB-P2 и последующие этапы пока являются roadmap, а не исполняемыми Sxx session
cards. Статусы S31-R1/S33 и старые краткие команды остаются в прежнем плане.

## Secret boundary

`.env` существует, но никогда не читается и не выводится исполнителем. Session может опираться только на задокументированные имена переменных и результат безопасного запуска приложения; значения секретов не попадают в context packet или handoff.
