"""Converter v1/v2 -> mia.json: every example of ``contracts/plugins/**/examples``."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from app.modules.plugins.declarative import ConversionError, convert_manifest

from tests.fakes.contracts import assert_mvp_valid, validator_module
from tests.plugins.support import ROOT, load_json

EXAMPLES = sorted((ROOT / "contracts/plugins").glob("v*/examples/*.json"))
SCRIPT = ROOT / "scripts/convert_manifest_v2.py"


def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("convert_manifest_v2_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_examples_are_found() -> None:
    assert [path.name for path in EXAMPLES] == [
        "day-planner.json",
        "focus-board.json",
        "focus-html.json",
    ]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_every_example_converts_to_a_valid_declarative_manifest(path: Path) -> None:
    converted = convert_manifest(load_json(path))
    manifest = converted.manifest
    assert_mvp_valid(manifest, "plugin-manifest")
    validator_module().check_manifest_semantics(manifest, path.name)
    source = load_json(path)
    assert manifest["id"] == source["id"] and manifest["version"] == source["version"]
    assert manifest["server"]["kind"] == "declarative"
    assert manifest["skills"] == ["SKILL.md"]
    assert converted.skill_md.startswith(f"---\nname: {source['id'].split('.', 1)[1]}\n")
    for name, policy in manifest["tools"].items():
        assert policy["risk"] in {"read", "write_reversible"}, name
    # ids, ui and permissions line up
    assert manifest["surfaces"]["tiles"][0]["resourceUri"] == f"ui://{source['id']}/tile"
    assert manifest["permissions"]["network"] == []
    assert manifest["permissions"]["llm"]["viaHost"] is ("ai.generate" in source["permissions"])


def test_v2_focus_board_mapping_matches_the_contract_example() -> None:
    converted = convert_manifest(load_json(ROOT / "contracts/plugins/v2/examples/focus-board.json"))
    manifest = converted.manifest
    source_description = load_json(ROOT / "contracts/plugins/v2/examples/focus-board.json")[
        "description"
    ]
    assert manifest["server"]["actions"] == [
        {
            "name": "set_focus",
            "description": "Set the main task in the focus board widget",
            "inputs": {"task": {"type": "string", "maxLength": 240}},
            "effect": "state.patch",
        }
    ]
    assert manifest["server"]["view"] == {"type": "fields", "fields": ["task"]}
    assert manifest["tools"] == {"set_focus": {"risk": "write_reversible", "voice": True}}
    assert manifest["listing"] == {
        "description": source_description,
        "category": "productivity",
        "author": "Вы",
    }
    assert "set_focus" in converted.skill_md


def test_v2_html_view_and_downloads_are_kept() -> None:
    source = load_json(ROOT / "contracts/plugins/v2/examples/focus-html.json")
    source["downloads"] = [
        {"platform": "web", "url": "https://app.example.com", "label": "Открыть"}
    ]
    manifest = convert_manifest(source, category="home").manifest
    assert manifest["server"]["view"]["type"] == "html"
    assert "mia.invoke('set_focus'" in manifest["server"]["view"]["html"]
    assert manifest["listing"]["category"] == "home"
    assert manifest["listing"]["downloads"] == source["downloads"]
    assert_mvp_valid(manifest, "plugin-manifest")


def test_v1_recipe_becomes_a_single_model_prompt_action() -> None:
    converted = convert_manifest(load_json(ROOT / "contracts/plugins/v1/examples/day-planner.json"))
    action = converted.manifest["server"]["actions"][0]
    assert action["name"] == "run" and action["effect"] == "model.prompt"
    assert "{{input}}" in action["prompt"]
    assert action["inputs"]["input"]["type"] == "string"
    assert converted.manifest["tools"]["run"] == {"risk": "read", "timeoutSeconds": 60}
    assert converted.manifest["permissions"]["llm"] == {"viaHost": True}
    assert converted.warnings


def v2(**changes: Any) -> dict[str, Any]:
    source: dict[str, Any] = load_json(ROOT / "contracts/plugins/v2/examples/focus-board.json")
    source.update(changes)
    return source


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ({"schemaVersion": "3.0"}, "unsupported schemaVersion"),
        (v2(permissions=["state.read"]), "state.write permission required"),
        (v2(permissions=["state.write"]), "state.read permission required"),
        (v2(id="community.1-bad"), "id"),
        (v2(id="other.thing"), "id"),
        (v2(extra=True), "extra"),
        (v2(actions=[]), "actions"),
        (
            v2(
                permissions=["state.read", "ai.generate"],
                actions=[
                    {
                        "name": "ask",
                        "description": "Ask the model",
                        "inputs": {"q": {"type": "string"}},
                        "effect": "model.prompt",
                        "prompt": "Q: {{q}}",
                    }
                ],
            ),
            "model.prompt needs",
        ),
    ],
)
def test_invalid_manifests_are_rejected_with_field_names(
    source: dict[str, Any], needle: str
) -> None:
    with pytest.raises(ConversionError) as error:
        convert_manifest(source)
    assert needle in str(error.value)
    assert "Set the main task" not in str(error.value), "no input echo"


def test_unknown_category_is_rejected() -> None:
    with pytest.raises(ConversionError, match="category"):
        convert_manifest(v2(), category="nope")


def test_cli_writes_package_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = script()
    out = tmp_path / "pkg"
    code = module.main(
        [str(ROOT / "contracts/plugins/v2/examples/focus-board.json"), "--out-dir", str(out)]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["id"] == "community.focus-board" and report["files"] == ["mia.json", "SKILL.md"]
    manifest = json.loads((out / "mia.json").read_text(encoding="utf-8"))
    assert_mvp_valid(manifest, "plugin-manifest")
    assert (out / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: focus-board")


def test_cli_prints_manifest_without_out_dir_and_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = script()
    assert module.main([str(ROOT / "contracts/plugins/v1/examples/day-planner.json")]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "community.day-planner"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert module.main([str(bad)]) == 2
    assert "error:" in capsys.readouterr().err
    assert module.main([str(tmp_path / "missing.json")]) == 2
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    assert module.main([str(array)]) == 2
