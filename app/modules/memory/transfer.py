"""Export (``/memories/*.md`` + JSON) and import parsing (Review §7.3).

Export layout (zip):

- ``memories/<category>.md`` — active facts of each category as a bullet list
  (the file-per-topic layout of Anthropic's memory tool);
- ``memories/people.md`` — active people with relation, aliases, identifiers;
- ``memories/preferences.md``;
- ``memories/suggested.md`` — facts still waiting for confirmation;
- ``memory.json`` — every record in ``memory.schema.json`` form.

Import accepts Markdown/plain text (one fact per bullet or line; ``#`` headings
naming a category route the bullets below them) or Mia JSON. Everything goes
to the review queue (``suggested``).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from typing import Any, Final, Literal

from app.core.ports.memory_provider import MemoryCategory
from app.modules.memory.domain import CATEGORIES, PersonInput, as_category, isoformat
from app.modules.memory.errors import ImportInvalidError, ImportTooLargeError
from app.modules.memory.service import ImportItems, LedgerSnapshot

IMPORT_MAX_CHARS: Final = 256_000
IMPORT_MAX_ITEMS: Final = 500
FACT_MAX_CHARS: Final = 2000
ImportFormat = Literal["markdown", "text", "json"]

CATEGORY_TITLES: Final[dict[MemoryCategory, str]] = {
    "profile": "Профиль",
    "work": "Работа",
    "home": "Дом",
    "health": "Здоровье",
    "finance": "Финансы",
    "interests": "Интересы",
    "other": "Другое",
}
_HEADING_ALIASES: Final[dict[str, MemoryCategory]] = {
    **{c: c for c in CATEGORIES},
    **{title.lower(): c for c, title in CATEGORY_TITLES.items()},
    "about me": "profile",
    "обо мне": "profile",
    "personal": "profile",
    "job": "work",
    "career": "work",
    "family": "home",
    "семья": "home",
    "hobbies": "interests",
    "хобби": "interests",
    "money": "finance",
    "деньги": "finance",
}
_BULLET = re.compile(r"^\s*(?:[-*•+]|\d{1,3}[.)])\s+(.*\S)\s*$")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*\S)\s*$")


def _escape(text: str) -> str:
    return " ".join(text.split())


def export_json(snapshot: LedgerSnapshot, *, exported_at: datetime) -> dict[str, Any]:
    return {
        "format": "mia.memory.v1",
        "exported_at": isoformat(exported_at),
        "facts": [fact.contract() for fact in snapshot.facts],
        "people": [person.contract() for person in snapshot.people],
        "preferences": [pref.contract() for pref in snapshot.preferences],
    }


def export_markdown(snapshot: LedgerSnapshot) -> dict[str, str]:
    files: dict[str, str] = {}
    for category in CATEGORIES:
        facts = [f for f in snapshot.facts if f.category == category and f.status == "active"]
        if facts:
            lines = [f"# {CATEGORY_TITLES[category]}", ""]
            lines += [f"- {_escape(f.text)}" for f in facts]
            files[f"memories/{category}.md"] = "\n".join(lines) + "\n"
    people = [p for p in snapshot.people if p.status == "active"]
    if people:
        lines = ["# Люди", ""]
        for person in people:
            line = f"- **{_escape(person.display_name)}**"
            if person.relation:
                line += f" — {_escape(person.relation)}"
            if person.aliases:
                line += f"; также: {', '.join(_escape(a) for a in person.aliases)}"
            for key, value in person.identifiers.items():
                line += f"; {key}: {value}"
            lines.append(line)
        files["memories/people.md"] = "\n".join(lines) + "\n"
    if snapshot.preferences:
        lines = ["# Предпочтения", ""]
        lines += [f"- {p.key}: {p.value}" for p in snapshot.preferences]
        files["memories/preferences.md"] = "\n".join(lines) + "\n"
    suggested = [f for f in snapshot.facts if f.status == "suggested"]
    if suggested:
        lines = ["# Ждут подтверждения", ""]
        lines += [f"- [{f.category}] {_escape(f.text)}" for f in suggested]
        files["memories/suggested.md"] = "\n".join(lines) + "\n"
    return files


def export_zip(snapshot: LedgerSnapshot, *, exported_at: datetime) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in export_markdown(snapshot).items():
            archive.writestr(name, body)
        ledger = export_json(snapshot, exported_at=exported_at)
        archive.writestr("memory.json", json.dumps(ledger, ensure_ascii=False, indent=2))
    return buffer.getvalue()


def _category_from_heading(title: str) -> MemoryCategory | None:
    return _HEADING_ALIASES.get(title.strip().strip("#").strip().lower())


def parse_text(content: str, *, markdown: bool) -> ImportItems:
    facts: list[tuple[str, MemoryCategory]] = []
    category: MemoryCategory = "other"
    for line in content.splitlines():
        if not line.strip():
            continue
        heading = _HEADING.match(line)
        if heading:
            category = _category_from_heading(heading.group(1)) or "other"
            continue
        bullet = _BULLET.match(line)
        if markdown and bullet is None:
            continue
        text = (bullet.group(1) if bullet else line).strip()
        if text:
            facts.append((text[:FACT_MAX_CHARS], category))
    return ImportItems(facts=facts, people=[])


def parse_json(content: str) -> ImportItems:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise ImportInvalidError() from None
    if not isinstance(data, dict):
        raise ImportInvalidError()
    facts: list[tuple[str, MemoryCategory]] = []
    for item in data.get("facts") or []:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        if item.get("status") == "rejected" or not item["text"].strip():
            continue
        category = item.get("category")
        facts.append(
            (
                item["text"].strip()[:FACT_MAX_CHARS],
                as_category(category) if category in CATEGORIES else "other",
            )
        )
    people: list[PersonInput] = []
    for item in data.get("people") or []:
        if not isinstance(item, dict) or not isinstance(item.get("display_name"), str):
            continue
        if item.get("status") == "rejected" or not item["display_name"].strip():
            continue
        people.append(PersonInput.from_payload(item))
    return ImportItems(facts=facts, people=people)


def parse_import(content: str, fmt: ImportFormat) -> ImportItems:
    if len(content) > IMPORT_MAX_CHARS:
        raise ImportTooLargeError()
    if fmt == "json":
        items = parse_json(content)
    else:
        items = parse_text(content, markdown=fmt == "markdown")
    if len(items.facts) + len(items.people) > IMPORT_MAX_ITEMS:
        raise ImportTooLargeError()
    if not items.facts and not items.people:
        raise ImportInvalidError("Nothing to import was found.")
    return items
