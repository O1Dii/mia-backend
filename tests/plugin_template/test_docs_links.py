"""Links in the plugin and SDK documentation resolve: files exist, anchors match a heading.

Only relative links and same-page anchors are checked; external URLs are not fetched.
Anchors follow GitHub's slug rule (lowercase, punctuation removed, spaces become hyphens).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.plugins.support import ROOT

DOCS = [
    ROOT / "docs" / "PLUGIN_DEVELOPMENT.md",
    ROOT / "docs" / "SDK_QUICKSTART.md",
    ROOT / "plugins" / "_template" / "README.md",
    ROOT / "sdk" / "python" / "README.md",
    ROOT / "sdk" / "js" / "README.md",
]

_FENCE = re.compile(r"^(```|~~~).*?^\1\s*$", re.MULTILINE | re.DOTALL)
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.MULTILINE)
_EXTERNAL = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)


def github_slug(heading: str) -> str:
    text = re.sub(r"[`*_~]|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", heading)
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    body = _FENCE.sub("", path.read_text("utf-8"))
    return {github_slug(match) for match in _HEADING.findall(body)}


def links_of(path: Path) -> list[str]:
    body = _FENCE.sub("", path.read_text("utf-8"))
    return [target for target in _LINK.findall(body) if not _EXTERNAL.match(target)]


def broken_links(doc: Path) -> list[str]:
    problems: list[str] = []
    for target in links_of(doc):
        file_part, _, anchor = target.partition("#")
        destination = (doc.parent / file_part).resolve() if file_part else doc
        if not destination.is_file():
            problems.append(f"{doc.relative_to(ROOT)}: missing file {target}")
            continue
        if anchor and destination.suffix == ".md" and anchor not in anchors_of(destination):
            problems.append(f"{doc.relative_to(ROOT)}: no heading for #{anchor} in {target}")
    return problems


def test_every_documented_file_exists() -> None:
    missing = [str(doc.relative_to(ROOT)) for doc in DOCS if not doc.is_file()]
    assert missing == []


def test_relative_links_and_anchors_resolve() -> None:
    problems = [problem for doc in DOCS for problem in broken_links(doc)]
    assert problems == []


def test_the_slug_rule_matches_github_for_cyrillic_and_code_headings() -> None:
    assert github_slug("Публикация (CLI)") == "публикация-cli"
    assert github_slug("`mia.json`: поля") == "miajson-поля"
    assert github_slug("Шаг 1. Скопируйте") == "шаг-1-скопируйте"
