"""Builtin plugin packages (in-process MCP servers), one directory per plugin.

Each package holds ``mia.json``, ``SKILL.md``, ``server.py`` (exposing
``build_server(services, manifest, plugin_dir)``) and ``ui/*.html`` (MCP App tiles).
Directories starting with ``_`` are shared helpers, not plugins.
"""
