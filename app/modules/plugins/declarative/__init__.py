"""Declarative (no-code) plugins: Mia itself is their MCP server."""

from app.modules.plugins.declarative.convert import (
    CATEGORIES,
    ConversionError,
    ConvertedPlugin,
    convert_manifest,
)
from app.modules.plugins.declarative.host import build_declarative_server

__all__ = [
    "CATEGORIES",
    "ConversionError",
    "ConvertedPlugin",
    "build_declarative_server",
    "convert_manifest",
]
