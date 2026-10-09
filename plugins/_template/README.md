# Шаблон плагина Mia

Стартовый пакет для нового плагина: `mia.json` (контракт `mia.plugin/1`), `SKILL.md`,
плитка `ui/status.html` и справочный MCP-сервер `server/plugin_server.py`. Полное руководство:
[docs/PLUGIN_DEVELOPMENT.md](../../docs/PLUGIN_DEVELOPMENT.md).

## Сделать свой плагин за час

1. Скопируйте каталог в `plugins/<имя>/` и поменяйте `id` (`команда.сервис`, например `team.notes-x`).
2. Замените `server/plugin_server.py` на свой MCP-сервер (Streamable HTTP, путь `/mcp`).
   Имена инструментов в `mia.json` должны совпасть с его `tools/list` один к одному.
3. Поменяйте `server.url` и `permissions.network` на адрес, где развёрнут сервер.
4. Опишите в `SKILL.md`, когда модель вызывает инструменты; плитку настройте в `ui/`.
5. Проверьте: `uv run python -m cli.mia_plugin validate plugins/<имя> --discover --token-env <ENV>`
   (нужен backend-хост с `TOOLS_MCP_HOST_ALLOWLIST`; без `--discover` сервер не опрашивается).
6. Публикацию выполняет администратор каталога командой `publish` на backend-хосте
   (см. раздел «Публикация» в руководстве).

Тесты шаблона: `uv run pytest tests/plugin_template -q`. Сервер в тестах работает в памяти;
публикация проверяется через `cli.mia_plugin` на тестовой базе.
