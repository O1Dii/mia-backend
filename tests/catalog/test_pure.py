"""Grants vocabulary, version ordering and publication checks that need no database."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from app.modules.plugins.builtin import BUILTIN_ROOT
from app.modules.plugins.builtin.bridge import CSP
from app.modules.plugins.catalog.package import (
    PackageFile,
    PackageReadError,
    PluginPackage,
    decode_files,
    encode_files,
    load_package,
)
from app.modules.plugins.catalog.validation import (
    package_problems,
    schema_problems,
    semantic_problems,
    ui_html_problems,
)
from app.modules.plugins.catalog.versions import InvalidVersionError, version_key
from app.modules.plugins.install.grants import diff_grants, required_grants

BUILTIN_IDS = sorted(path.name for path in BUILTIN_ROOT.iterdir() if (path / "mia.json").is_file())
EXAMPLES = Path(__file__).resolve().parents[2] / "contracts/mvp/v1/examples/plugin-manifest"


def linear() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((EXAMPLES / "linear.mia.json").read_text("utf-8")))


def package_of(manifest: dict[str, Any], files: dict[str, bytes] | None = None) -> PluginPackage:
    return PluginPackage(
        manifest=manifest, files={p: PackageFile(c) for p, c in (files or {}).items()}
    )


def linear_package() -> PluginPackage:
    return package_of(
        linear(),
        {
            "SKILL.md": b"---\ndescription: Linear\n---\n",
            "assets/icon.png": b"\x89PNG",
            "assets/s1.png": b"\x89PNG",
            "ui/my-issues.html": b"<html><head><!--mia:bridge--></head><body>ok</body></html>",
        },
    )


# ------------------------------------------------------------------ grants


def test_grants_follow_tools_and_permissions() -> None:
    manifest = linear()
    assert required_grants(manifest) == {
        "tools.read",
        "tools.write_reversible",
        "tools.write_external",
        "network:mcp.linear.app",
        "notify",
    }
    manifest["permissions"].update(
        {"publicWeb": True, "llm": {"viaHost": True}, "memory": ["context", "read:work"]}
    )
    manifest["permissions"]["schedules"] = True
    assert required_grants(manifest) >= {
        "network.public_web",
        "llm.generate",
        "memory.context",
        "memory.read:work",
        "schedules",
    }


def test_grant_diff_flags_only_additions_as_expansion() -> None:
    narrower = diff_grants({"tools.read", "notify"}, {"tools.read"})
    assert (narrower.added, narrower.removed, narrower.expanded) == ((), ("notify",), False)
    wider = diff_grants({"tools.read"}, {"tools.read", "network:a.example"})
    assert wider.added == ("network:a.example",) and wider.expanded


def test_grants_ignore_malformed_manifests() -> None:
    assert required_grants({}) == frozenset()
    assert required_grants({"tools": {"x": "nope"}, "permissions": {"network": "x"}}) == frozenset()


# ---------------------------------------------------------------- versions


def test_version_keys_sort_like_semver() -> None:
    ordered = ["0.9.0", "1.0.0-rc1", "1.0.0", "1.0.1", "1.2.0", "1.10.0", "2.0.0"]
    assert sorted(ordered, key=version_key) == ordered
    for bad in ("1.0", "v1.0.0", "01.0.0", ""):
        with pytest.raises(InvalidVersionError):
            version_key(bad)


# --------------------------------------------------------------- validation


@pytest.mark.parametrize("name", BUILTIN_IDS)
def test_every_builtin_package_passes_publication_checks(name: str) -> None:
    package = load_package(BUILTIN_ROOT / name)
    assert package_problems(package) == []
    assert "SKILL.md" in package.files and not any(p.endswith(".py") for p in package.files)


def test_the_contract_examples_are_valid_manifests() -> None:
    for path in sorted(EXAMPLES.glob("*.mia.json")):
        manifest = json.loads(path.read_text("utf-8"))
        assert schema_problems(manifest) == [], path.name


def test_schema_violations_are_reported_with_their_path() -> None:
    manifest = linear()
    manifest["tools"]["BadName"] = {"risk": "read"}
    manifest["permissions"]["extra"] = True
    problems = schema_problems(manifest)
    assert problems and all(p.code == "manifest_schema" for p in problems)


def test_remote_host_must_be_allow_listed_and_tiles_must_use_the_plugin_uri() -> None:
    manifest = linear()
    manifest["permissions"]["network"] = ["other.example.com"]
    manifest["surfaces"]["tiles"][0]["resourceUri"] = "ui://team.other/my-issues"
    manifest["surfaces"]["tiles"][0]["refresh"]["tool"] = "create_issue"
    paths = {p.path for p in semantic_problems(manifest)}
    assert paths == {"server.url", "tiles.my-issues"}


def declarative(tools: list[str]) -> dict[str, Any]:
    return {
        "schemaVersion": "mia.plugin/1",
        "id": "community.focus-board",
        "version": "1.0.0",
        "name": "Focus board",
        "listing": {"description": "Main task widget for focus", "category": "productivity"},
        "server": {
            "kind": "declarative",
            "actions": [
                {
                    "name": "set_focus",
                    "description": "Set the main task",
                    "inputs": {"task": {"type": "string"}},
                    "effect": "state.patch",
                }
            ],
            "view": {"type": "fields", "fields": ["task"]},
        },
        "tools": {name: {"risk": "write_reversible"} for name in tools},
        "permissions": {"network": [], "llm": {"viaHost": False}, "memory": [], "notify": False},
    }


def test_declarative_tools_may_add_only_the_app_only_get_state() -> None:
    assert semantic_problems(declarative(["set_focus"])) == []
    assert semantic_problems(declarative(["set_focus", "get_state"])) == []
    assert [p.path for p in semantic_problems(declarative(["set_focus", "other"]))] == ["tools"]
    assert [p.path for p in semantic_problems(declarative(["other"]))] == ["tools"]


def test_public_web_is_part_of_the_manifest_schema_but_not_for_remote_servers() -> None:
    manifest = linear()
    manifest["permissions"]["publicWeb"] = True
    assert schema_problems(manifest) == []
    assert [p.path for p in semantic_problems(manifest)] == ["permissions.publicWeb"]


def test_missing_referenced_files_are_listed() -> None:
    package = package_of(linear(), {"ui/my-issues.html": b"<p>x</p>"})
    missing = {p.path for p in package_problems(package) if p.code == "package_file_missing"}
    assert missing == {"skills[0]", "listing.icon", "listing.screenshots[0]"}


@pytest.mark.parametrize(
    ("html", "fragment"),
    [
        ('<script src="https://evil.example/x.js"></script>', "external <script"),
        ('<link rel="stylesheet" href="https://evil.example/a.css">', "external <link"),
        ('<img src="https://evil.example/p.png">', "must be a data: URL"),
        ('<iframe src="https://evil.example"></iframe>', "<iframe>"),
        ("<base href='https://evil.example/'>", "<base>"),
        ('<form action="https://evil.example/post"></form>', "<form action>"),
        ('<meta http-equiv="refresh" content="0;url=https://evil.example">', "refresh"),
        ('<meta http-equiv="Content-Security-Policy" content="default-src *">', "own Content-Sec"),
        ('<div style="background:url(https://evil.example/a.png)">x</div>', "style url()"),
        ("<style>@import 'https://evil.example/a.css';</style>", "CSS url()/@import"),
        ("<style>a{background:url(https://evil.example/a.png)}</style>", "CSS url()/@import"),
        ('<object data="x"></object>', "<object>"),
    ],
)
def test_unsafe_tile_markup_is_rejected(html: str, fragment: str) -> None:
    problems = ui_html_problems(f"<html><head></head><body>{html}</body></html>", "ui/x.html")
    assert any(fragment in p.message for p in problems), problems


def test_safe_markup_passes_and_the_served_resource_needs_the_host_csp() -> None:
    safe = (
        '<html><body><img src="data:image/png;base64,AAAA">'
        '<button onclick="mia.call(1)">x</button></body></html>'
    )
    assert ui_html_problems(safe, "ui/x.html") == []
    served = (
        f'<html><head><meta http-equiv="Content-Security-Policy" content="{CSP}"></head></html>'
    )
    assert ui_html_problems(served, "t", require_host_csp=True) == []
    assert [p.message for p in ui_html_problems(safe, "t", require_host_csp=True)] == [
        "the served resource lacks the host Content-Security-Policy"
    ]


# ------------------------------------------------------------------ package


def test_package_digest_covers_manifest_and_files() -> None:
    first, same = linear_package(), linear_package()
    assert first.digest() == same.digest()
    changed = copy.deepcopy(linear_package().manifest)
    changed["version"] = "0.1.1"
    assert (
        package_of(changed, {p: f.content for p, f in first.files.items()}).digest()
        != first.digest()
    )
    files = {p: f.content for p, f in first.files.items()} | {"SKILL.md": b"changed"}
    assert package_of(first.manifest, files).digest() != first.digest()


def test_files_round_trip_through_the_stored_json_form() -> None:
    files = {"a.md": PackageFile("текст".encode()), "b.png": PackageFile(b"\x89PNG\x00\xff")}
    stored = encode_files(files)
    assert stored["a.md"]["encoding"] == "utf8" and stored["b.png"]["encoding"] == "base64"
    assert {p: f.content for p, f in decode_files(stored).items()} == {
        p: f.content for p, f in files.items()
    }


def test_loader_skips_code_rejects_symlinks_and_oversized_files(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    (root / "ui").mkdir(parents=True)
    (root / "mia.json").write_text(json.dumps(linear()), "utf-8")
    (root / "server.py").write_text("print('code')", "utf-8")
    (root / "ui" / "a.html").write_text("<p>a</p>", "utf-8")
    (root / "notes.exe").write_bytes(b"x")
    assert sorted(load_package(root).files) == ["ui/a.html"]

    (root / "link.md").symlink_to(root / "ui" / "a.html")
    with pytest.raises(PackageReadError, match="symbolic"):
        load_package(root)
    (root / "link.md").unlink()
    (root / "big.md").write_bytes(b"x" * (512 * 1024 + 1))
    with pytest.raises(PackageReadError, match="exceeds"):
        load_package(root)
    (root / "big.md").unlink()
    (root / "mia.json").write_text("[]", "utf-8")
    with pytest.raises(PackageReadError, match="object"):
        load_package(root)
    with pytest.raises(PackageReadError, match="missing"):
        load_package(tmp_path / "nowhere")
