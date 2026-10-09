"""Composition, error-code registry, export/import parsing (no database needed)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.ports.memory_provider import MemorySource
from app.main import create_app
from app.modules.memory import MemoryStack
from app.modules.memory.domain import Fact, Person
from app.modules.memory.errors import MEMORY_CODES, ImportInvalidError, ImportTooLargeError
from app.modules.memory.service import LedgerSnapshot
from app.modules.memory.transfer import (
    IMPORT_MAX_ITEMS,
    export_json,
    export_markdown,
    parse_import,
)

from tests.fakes import assert_mvp_valid

REGISTRY = Path("contracts/mvp/v1/error-codes.json")
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def test_application_wires_memory_into_runs_and_tools() -> None:
    app = create_app(Settings(app_env="test"))
    stack = app.state.memory
    assert isinstance(stack, MemoryStack)
    assert app.state.runs.engine.memory is stack.service
    assert stack.tools.channel is app.state.tools.device_channel
    names = {tool.name for tool in app.state.tools.core_tools.tools()}
    assert {"memory.remember", "people.resolve"} <= names
    paths = set(app.openapi()["paths"])
    assert {
        "/v1/memory/facts",
        "/v1/memory/facts/{fact_id}",
        "/v1/memory/people",
        "/v1/memory/people/{person_id}",
        "/v1/memory/preferences",
        "/v1/memory/preferences/{key}",
        "/v1/memory/context",
        "/v1/memory/export",
        "/v1/memory/import",
        "/v1/memory/settings",
        "/v1/memory/access-log",
        "/v1/memory",
    } <= paths


def test_memory_codes_are_registered() -> None:
    registry = {entry["code"]: entry for entry in json.loads(REGISTRY.read_text("utf-8"))}
    for code, (status, retryable, message) in MEMORY_CODES.items():
        entry = registry[code]
        assert entry["owner"] == "M17"
        assert (entry["http_status"], entry["retryable"], entry["public_message"]) == (
            status,
            retryable,
            message,
        )


def snapshot() -> LedgerSnapshot:
    fact = Fact(
        id=uuid.uuid4(),
        text="Меня зовут Алексей",
        category="profile",
        status="active",
        sensitivity="normal",
        scope="user",
        source=MemorySource("user_said"),
        created_at=NOW,
        updated_at=NOW,
        last_used_at=None,
    )
    pending = Fact(
        id=uuid.uuid4(),
        text="Любит джаз",
        category="interests",
        status="suggested",
        sensitivity="normal",
        scope="user",
        source=MemorySource("turn", "run"),
        created_at=NOW,
        updated_at=NOW,
        last_used_at=None,
    )
    person = Person(
        id=uuid.uuid4(),
        display_name="Яна",
        aliases=("Янка",),
        relation="жена",
        identifiers={"phone": "+79161234567"},
        trusted=False,
        status="active",
        sensitivity="normal",
        source=MemorySource("user_said"),
        created_at=NOW,
        updated_at=NOW,
        last_used_at=NOW,
    )
    return LedgerSnapshot(facts=[fact, pending], people=[person], preferences=[])


def test_export_files_and_json() -> None:
    files = export_markdown(snapshot())
    assert files["memories/profile.md"] == "# Профиль\n\n- Меня зовут Алексей\n"
    assert "- **Яна** — жена; также: Янка; phone: +79161234567" in files["memories/people.md"]
    assert files["memories/suggested.md"].endswith("- [interests] Любит джаз\n")
    assert "memories/interests.md" not in files  # only confirmed facts are «known»
    body = export_json(snapshot(), exported_at=NOW)
    assert body["format"] == "mia.memory.v1"
    for record in body["facts"] + body["people"]:
        assert_mvp_valid(record, "memory")


def test_import_markdown_routes_headings_to_categories() -> None:
    items = parse_import(
        "# Work\n- Пишет на Kotlin\n## Хобби\n* Бег по утрам\n1. Шахматы\nне пункт\n# Misc\n- x",
        "markdown",
    )
    assert items.facts == [
        ("Пишет на Kotlin", "work"),
        ("Бег по утрам", "interests"),
        ("Шахматы", "interests"),
        ("x", "other"),
    ]
    text = parse_import("Первый факт\n\nВторой факт\n", "text")
    assert [f for f, _ in text.facts] == ["Первый факт", "Второй факт"]


def test_import_json_skips_rejected_and_bad_items() -> None:
    content = json.dumps(
        {
            "facts": [
                {"text": "Да", "category": "work"},
                {"text": "Нет", "status": "rejected"},
                {"text": "Странная категория", "category": "weird"},
                {"nope": 1},
            ],
            "people": [{"display_name": "Яна", "relation": "жена"}, {"display_name": ""}],
        }
    )
    items = parse_import(content, "json")
    assert items.facts == [("Да", "work"), ("Странная категория", "other")]
    assert [p.display_name for p in items.people] == ["Яна"]


@pytest.mark.parametrize(
    ("content", "fmt", "error"),
    [
        ("[]", "json", ImportInvalidError),
        ("{", "json", ImportInvalidError),
        ("просто текст без пунктов", "markdown", ImportInvalidError),
        ("x" * 300_000, "text", ImportTooLargeError),
        ("\n".join(f"- {i}" for i in range(IMPORT_MAX_ITEMS + 1)), "markdown", ImportTooLargeError),
    ],
)
def test_import_rejects(content: str, fmt: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        parse_import(content, fmt)  # type: ignore[arg-type]
