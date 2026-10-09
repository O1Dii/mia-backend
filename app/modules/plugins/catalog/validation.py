"""Publication checks that need no server: schema, cross-field rules, files, UI markup.

``mia.json`` is validated against ``contracts/mvp/v1/plugin-manifest.schema.json`` (the
same schema the contract tests use). The cross-field rules mirror
``contracts/scripts/validate_contracts.py::check_manifest_semantics`` with the two
follow-ups of MW2: a declarative plugin may also list the app-only ``get_state`` tool
(OI-M14-001) and ``permissions.publicWeb`` is part of the schema (OI-M14-002).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.modules.plugins.builtin.bridge import CSP
from app.modules.plugins.catalog.package import PluginPackage

CONTRACTS_DIR: Final = Path(__file__).resolve().parents[4] / "contracts" / "mvp" / "v1"
_ID_PREFIX: Final = "https://contracts.mia.local/mvp/v1/"
APP_ONLY_TOOL: Final = "get_state"
"""Hidden tool every declarative server adds for its tile (never offered to a model)."""


class ContractsUnavailableError(RuntimeError):
    """``contracts/mvp/v1`` is not next to the application (image without contracts)."""


@dataclass(frozen=True, slots=True)
class Problem:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}: " if self.path else ""
        return f"[{self.code}] {where}{self.message}"


@lru_cache(maxsize=1)
def _manifest_validator() -> Draft202012Validator:
    if not CONTRACTS_DIR.is_dir():
        raise ContractsUnavailableError("contracts/mvp/v1 is not available")
    resources = []
    for path in sorted(CONTRACTS_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text("utf-8"))
        resources.append(
            (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
        )
    registry: Registry[Any] = Registry().with_resources(resources)
    return Draft202012Validator(
        {"$ref": _ID_PREFIX + "plugin-manifest.schema.json"},
        registry=registry,
        format_checker=FormatChecker(),
    )


def schema_problems(manifest: dict[str, Any]) -> list[Problem]:
    errors = sorted(_manifest_validator().iter_errors(manifest), key=lambda e: list(e.path))
    return [
        Problem("manifest_schema", "/".join(str(part) for part in error.path), error.message[:300])
        for error in errors[:20]
    ]


def semantic_problems(manifest: dict[str, Any]) -> list[Problem]:
    """Cross-field rules JSON Schema cannot express (manifest already schema-valid)."""
    problems: list[Problem] = []
    tools = set(manifest["tools"])
    server = manifest["server"]
    network = set(manifest["permissions"]["network"])
    if server["kind"] == "remote":
        host = urlsplit(server["url"]).hostname
        if host not in network:
            problems.append(
                Problem(
                    "manifest_semantics",
                    "server.url",
                    f"host {host} must be in permissions.network",
                )
            )
    if server["kind"] == "declarative":
        actions = {action["name"] for action in server["actions"]}
        if not actions <= tools or not tools <= actions | {APP_ONLY_TOOL}:
            problems.append(
                Problem(
                    "manifest_semantics",
                    "tools",
                    f"declarative actions {sorted(actions)} must equal tools {sorted(tools)}"
                    f" (get_state is the only extra tool allowed)",
                )
            )
    for tile in manifest.get("surfaces", {}).get("tiles", []):
        refresh = tile.get("refresh")
        if refresh and refresh["tool"] not in tools:
            problems.append(
                Problem("manifest_semantics", f"tiles.{tile['id']}", "refreshes an unknown tool")
            )
        elif refresh and manifest["tools"][refresh["tool"]]["risk"] != "read":
            problems.append(
                Problem(
                    "manifest_semantics", f"tiles.{tile['id']}", "must refresh with a read tool"
                )
            )
        prefix = f"ui://{manifest['id']}/"
        if not tile["resourceUri"].startswith(prefix):
            problems.append(
                Problem(
                    "manifest_semantics",
                    f"tiles.{tile['id']}",
                    f"resourceUri must start with {prefix}",
                )
            )
    if manifest.get("permissions", {}).get("publicWeb") and server["kind"] == "remote":
        problems.append(
            Problem(
                "manifest_semantics",
                "permissions.publicWeb",
                "only builtin plugins may read arbitrary public pages",
            )
        )
    return problems


def referenced_files(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """``(where, path)`` of every package file the manifest points at."""
    refs: list[tuple[str, str]] = [
        (f"skills[{i}]", p) for i, p in enumerate(manifest.get("skills", []))
    ]
    listing = manifest.get("listing", {})
    if listing.get("icon"):
        refs.append(("listing.icon", listing["icon"]))
    refs.extend(
        (f"listing.screenshots[{i}]", p) for i, p in enumerate(listing.get("screenshots", []))
    )
    for tile in manifest.get("surfaces", {}).get("tiles", []):
        if tile.get("bundled"):
            refs.append((f"tiles.{tile['id']}.bundled", tile["bundled"]))
    return refs


class _UiScan(HTMLParser):
    """Flags markup that would escape the sandbox the host CSP builds around a tile."""

    def __init__(self) -> None:
        super().__init__()
        self.issues: list[str] = []
        self.host_csp = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: (value or "") for name, value in attrs}
        if tag in {"iframe", "frame", "frameset", "embed", "object", "applet", "base"}:
            self.issues.append(f"<{tag}> is not allowed")
        elif tag == "script" and values.get("src"):
            self.issues.append("external <script src> is not allowed")
        elif tag == "link" and values.get("href") and not values["href"].startswith("data:"):
            self.issues.append("external <link href> is not allowed")
        elif tag in {"img", "source", "audio", "video", "track", "input"} and values.get("src"):
            if not values["src"].startswith("data:"):
                self.issues.append(f"<{tag} src> must be a data: URL")
        elif tag == "form" and values.get("action"):
            self.issues.append("<form action> is not allowed")
        elif tag == "meta":
            equiv = values.get("http-equiv", "").lower()
            if equiv == "content-security-policy":
                if values.get("content") == CSP:
                    self.host_csp = True
                else:
                    self.issues.append("a plugin must not set its own Content-Security-Policy")
            elif equiv == "refresh":
                self.issues.append("<meta http-equiv=refresh> is not allowed")
        style = values.get("style", "")
        if re.search(r"url\(\s*['\"]?(?!data:)", style, flags=re.IGNORECASE):
            self.issues.append("style url() must be a data: URL")


_CSS_URL = re.compile(r"url\(\s*['\"]?(?!data:|#)[^)]", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import", re.IGNORECASE)


def ui_html_problems(html: str, where: str, *, require_host_csp: bool = False) -> list[Problem]:
    """UI markup rules; ``require_host_csp`` also demands the host CSP (a served resource)."""
    scan = _UiScan()
    scan.feed(html)
    scan.close()
    issues = list(scan.issues)
    if _CSS_URL.search(html) or _CSS_IMPORT.search(html):
        issues.append("CSS url()/@import must not load external resources")
    if require_host_csp and not scan.host_csp:
        issues.append("the served resource lacks the host Content-Security-Policy")
    return [Problem("ui_unsafe", where, issue) for issue in issues]


def package_problems(package: PluginPackage) -> list[Problem]:
    """Everything checkable without a running server; empty list means publishable."""
    manifest = package.manifest
    problems = schema_problems(manifest)
    if problems:
        return problems
    problems.extend(semantic_problems(manifest))
    for where, path in referenced_files(manifest):
        if path not in package.files:
            problems.append(Problem("package_file_missing", where, f"{path} is not in the package"))
    for path in sorted(package.files):
        if path.lower().endswith(".html"):
            text = package.text(path)
            if text is None:
                problems.append(Problem("ui_unsafe", path, "HTML must be UTF-8"))
            else:
                problems.extend(ui_html_problems(text, path))
    return problems
