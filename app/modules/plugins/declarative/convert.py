"""Convert POC manifests (recipe v1, application v2) to ``mia.json`` (``mia.plugin/1``).

The result has ``server.kind: declarative`` and runs on Mia's own MCP server
(:mod:`app.modules.plugins.declarative.host`). The rules of the POC runtime stay the same:
``state.patch`` needs ``state.write``, ``model.prompt`` needs ``ai.generate`` and a string
``input`` referenced as ``{{input}}``, ``state.read`` is always required.

Mapping notes: v2 ``skill`` becomes ``SKILL.md`` (returned as text, the CLI writes it);
v2 ``downloads`` become ``listing.downloads``; ``state.patch`` -> risk ``write_reversible``
and voice-safe, ``model.prompt`` -> risk ``read`` (it spends the user's model budget but
has no external effect), not voice; v1 recipes become one ``run`` action.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}(\.[a-z][a-z0-9-]{1,47}){1,2}$")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
CATEGORIES = ("work", "productivity", "communication", "knowledge", "home", "utilities", "fun")
RECIPE_INPUT_MAX = 8000


class ConversionError(ValueError):
    """The source manifest is invalid or cannot be expressed as ``mia.json``."""


@dataclass(frozen=True)
class ConvertedPlugin:
    manifest: dict[str, Any]
    skill_md: str
    warnings: list[str] = field(default_factory=list)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _Input(_Strict):
    type: Literal["string", "integer", "boolean"]
    description: str = Field(default="", max_length=300)
    maxLength: int = Field(default=1000, ge=1, le=16000)
    minimum: int = Field(default=0, ge=-1_000_000)
    maximum: int = Field(default=1_000_000, le=1_000_000)


class _Action(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    description: str = Field(min_length=5, max_length=300)
    inputs: dict[str, _Input] = Field(max_length=10)
    effect: Literal["state.patch", "model.prompt"]
    prompt: str | None = Field(default=None, max_length=8000)


class _View(_Strict):
    type: Literal["fields", "html"] = "fields"
    fields: list[str] = Field(default_factory=list, max_length=10)
    html: str = Field(default="", max_length=24000)


class _Download(_Strict):
    platform: Literal["web", "android", "ios", "desktop"]
    url: str = Field(pattern=r"^https://[^\s]+$", max_length=2048)
    label: str = Field(min_length=1, max_length=60)


class _Common(_Strict):
    id: str = Field(pattern=r"^community\.[a-z0-9-]{2,48}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    name: str = Field(min_length=2, max_length=60)
    description: str = Field(min_length=10, max_length=240)
    author: str = Field(min_length=1, max_length=60)


class _AppV2(_Common):
    schemaVersion: Literal["2.0"]
    skill: str = Field(min_length=20, max_length=6000)
    permissions: list[Literal["state.read", "state.write", "ai.generate"]] = Field(max_length=3)
    actions: list[_Action] = Field(min_length=1, max_length=10)
    view: _View
    downloads: list[_Download] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def check_rules(self) -> _AppV2:
        if len({action.name for action in self.actions}) != len(self.actions):
            raise ValueError("action names must be unique")
        if "state.read" not in self.permissions:
            raise ValueError("state.read permission required")
        for action in self.actions:
            if any(spec.minimum > spec.maximum for spec in action.inputs.values()):
                raise ValueError("invalid integer range")
            if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) for key in action.inputs):
                raise ValueError("invalid input name")
            if action.effect == "state.patch" and "state.write" not in self.permissions:
                raise ValueError("state.write permission required")
            if action.effect == "model.prompt" and (
                "ai.generate" not in self.permissions
                or not action.prompt
                or "{{input}}" not in action.prompt
                or list(action.inputs) != ["input"]
                or action.inputs["input"].type != "string"
            ):
                raise ValueError("model.prompt needs ai.generate, string input and {{input}}")
        return self


class _RecipeV1(_Common):
    schemaVersion: Literal["1.0"]
    runtime: Literal["prompt"]
    permissions: list[Literal["ai.generate"]] = Field(min_length=1, max_length=1)
    prompt: str = Field(min_length=10, max_length=8000, pattern=r"\{\{input\}\}")


def convert_manifest(
    source: Mapping[str, Any], *, category: str = "productivity"
) -> ConvertedPlugin:
    """Convert a v1 recipe or v2 application to a declarative ``mia.json`` plus ``SKILL.md``."""
    if category not in CATEGORIES:
        raise ConversionError(f"unknown category {category!r}")
    version = source.get("schemaVersion")
    try:
        if version == "2.0":
            return _convert_v2(_AppV2.model_validate(dict(source)), category)
        if version == "1.0":
            return _convert_v1(_RecipeV1.model_validate(dict(source)), category)
    except ValidationError as error:
        # Field paths and our own rule messages only; never the rejected values.
        problems = sorted(
            {
                ".".join(str(part) for part in item["loc"])
                or str(item["msg"]).removeprefix("Value error, ")
                for item in error.errors()
            }
        )
        raise ConversionError("invalid manifest: " + "; ".join(problems)) from None
    raise ConversionError(f"unsupported schemaVersion {version!r} (expected '1.0' or '2.0')")


def _skill_md(slug: str, description: str, body: str) -> str:
    one_line = " ".join(description.split())
    return f"---\nname: {slug}\ndescription: {one_line}\n---\n\n{body.strip()}\n"


def _envelope(
    common: _Common, category: str, server: dict[str, Any], tools: dict[str, Any], llm: bool
) -> dict[str, Any]:
    if not PLUGIN_ID.fullmatch(common.id):
        raise ConversionError(
            f"id {common.id!r} does not fit the mia.json id pattern (the part after 'community.' "
            "must start with a letter)"
        )
    if not SEMVER.fullmatch(common.version):
        raise ConversionError(f"version {common.version!r} is not a plain semantic version")
    return {
        "schemaVersion": "mia.plugin/1",
        "id": common.id,
        "version": common.version,
        "name": common.name,
        "listing": {
            "description": common.description,
            "category": category,
            "author": common.author,
        },
        "server": server,
        "skills": ["SKILL.md"],
        "tools": tools,
        "surfaces": {
            "tiles": [
                {
                    "id": "main",
                    "title": common.name,
                    "resourceUri": f"ui://{common.id}/tile",
                    "sizes": ["2x1", "2x2"],
                    "mobile": "card",
                }
            ]
        },
        "permissions": {"network": [], "llm": {"viaHost": llm}, "memory": [], "notify": False},
    }


def _convert_v2(app: _AppV2, category: str) -> ConvertedPlugin:
    actions: list[dict[str, Any]] = []
    tools: dict[str, Any] = {}
    for action in app.actions:
        item: dict[str, Any] = {
            "name": action.name,
            "description": action.description,
            "inputs": {key: _input(spec) for key, spec in action.inputs.items()},
            "effect": action.effect,
        }
        if action.effect == "model.prompt":
            item["prompt"] = action.prompt
            tools[action.name] = {"risk": "read", "timeoutSeconds": 60}
        else:
            tools[action.name] = {"risk": "write_reversible", "voice": True}
        actions.append(item)
    view: dict[str, Any] = {"type": app.view.type}
    if app.view.type == "fields":
        view["fields"] = list(app.view.fields)
    else:
        view["html"] = app.view.html
    server = {"kind": "declarative", "actions": actions, "view": view}
    manifest = _envelope(app, category, server, tools, "ai.generate" in app.permissions)
    if app.downloads:
        manifest["listing"]["downloads"] = [download.model_dump() for download in app.downloads]
    slug = app.id.split(".", 1)[1]
    return ConvertedPlugin(manifest, _skill_md(slug, app.description, app.skill))


def _input(spec: _Input) -> dict[str, Any]:
    result: dict[str, Any] = {"type": spec.type}
    if spec.description:
        result["description"] = spec.description
    if spec.type == "string":
        result["maxLength"] = spec.maxLength
    elif spec.type == "integer":
        result.update(minimum=spec.minimum, maximum=spec.maximum)
    return result


def _convert_v1(recipe: _RecipeV1, category: str) -> ConvertedPlugin:
    action = {
        "name": "run",
        "description": recipe.description,
        "inputs": {
            "input": {
                "type": "string",
                "description": "Текст пользователя для обработки",
                "maxLength": RECIPE_INPUT_MAX,
            }
        },
        "effect": "model.prompt",
        "prompt": recipe.prompt,
    }
    server = {
        "kind": "declarative",
        "actions": [action],
        "view": {"type": "fields", "fields": ["answer"]},
    }
    manifest = _envelope(
        recipe, category, server, {"run": {"risk": "read", "timeoutSeconds": 60}}, True
    )
    slug = recipe.id.split(".", 1)[1]
    body = (
        f"# {recipe.name}\n\n{recipe.description}\n\nКогда пользователь просит об этом, "
        "вызови `run` с `input` - его текстом. Ответ приходит в `answer`."
    )
    return ConvertedPlugin(
        manifest,
        _skill_md(slug, recipe.description, body),
        ["v1 recipe converted to the single action 'run'"],
    )
