"""Pure helpers: stems, minimization, person extraction, name matching, contacts parsing."""

from __future__ import annotations

import pytest
from app.modules.memory.domain import clean_identifiers, normalize_phone
from app.modules.memory.text import (
    contains_identifier,
    extract_person,
    minimize,
    name_score,
    search_terms,
    stem,
)
from app.modules.memory.tools import Contact, ContactsCache, parse_contacts


def test_inflected_names_share_a_stem() -> None:
    assert stem("Яна") == stem("Яне") == stem("Яну") == stem("Яной")
    assert stem("жена") == stem("жене") == stem("женой")
    assert stem("coffee") == "coffee"
    assert stem("meetings") == stem("meeting")


def test_search_terms_drop_stopwords_and_keep_order() -> None:
    assert search_terms("Запомни, что Яна — моя жена") == [stem("Яна"), stem("жена")]
    assert search_terms("") == []


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("Телефон Яны +7 (916) 123-45-67", "123-45-67"),
        ("Её номер 89161234567", "89161234567"),
        ("Почта yana@example.com", "yana@example.com"),
        ("Ник в телеграме @yana_petrova", "@yana_petrova"),
        ("telegram id 123456789", "123456789"),
    ],
)
def test_minimize_removes_identifiers(text: str, leak: str) -> None:
    assert contains_identifier(text)
    cleaned = minimize(text)
    assert leak not in cleaned
    assert not contains_identifier(cleaned)


def test_minimize_keeps_ordinary_text() -> None:
    text = "Пьёт кофе без сахара, встаёт в 7:30, дочке 5 лет"
    assert minimize(text) == text


@pytest.mark.parametrize(
    ("text", "name", "relation"),
    [
        ("Запомни, что Яна — моя жена", "Яна", "жена"),
        ("Яна - моя жена.", "Яна", "жена"),
        ("запомни: Олег Иванов — мой начальник", "Олег Иванов", "начальник"),
        ("Remember that Yana is my wife", "Yana", "wife"),
    ],
)
def test_extract_person(text: str, name: str, relation: str) -> None:
    draft = extract_person(text)
    assert draft is not None
    assert (draft.display_name, draft.relation) == (name, relation)


def test_extract_person_ignores_other_facts() -> None:
    assert extract_person("Я люблю кофе без сахара") is None
    assert extract_person("Работаю в Минске") is None


def test_name_score_orders_exact_stem_relation() -> None:
    names = ["Яна", "Янка"]
    assert name_score("Яна", names, "жена") == 3
    assert name_score("Яне", names, "жена") == 2
    assert name_score("жене", names, "жена") == 1
    assert name_score("Олег", names, "жена") == 0


def test_identifiers_are_validated() -> None:
    assert normalize_phone("+7 (916) 123-45-67") == "+79161234567"
    assert normalize_phone("8 916 123 45 67") is None
    assert clean_identifiers(
        {"phone": "+375 29 111-22-33", "telegram_username": "@yana_p", "x": 1, "email": "bad"}
    ) == {"phone": "+375291112233", "telegram_username": "yana_p"}


def test_parse_contacts_is_tolerant_and_bounded() -> None:
    contacts = parse_contacts(
        {
            "contacts": [
                {"display_name": "Яна Петрова", "phones": ["+7 916 123-45-67"]},
                {"name": "Яна Работа", "phone": "+375291112233", "email": "y@example.com"},
                {"name": ""},
                "junk",
            ]
            * 3
        }
    )
    assert len(contacts) == 5
    assert contacts[0] == Contact("Яна Петрова", {"phone": "+79161234567"})
    assert contacts[1].identifiers == {"phone": "+375291112233", "email": "y@example.com"}
    assert parse_contacts(None) == []
    assert parse_contacts({"contacts": "nope"}) == []


def test_contacts_cache_is_per_run_and_expires() -> None:
    now = [0.0]
    cache = ContactsCache(ttl_s=10, max_runs=2, monotonic=lambda: now[0])
    refs = cache.put(user_id="u", run_id="r1", contacts=[Contact("Яна", {})])
    assert refs == ["contact:1"]
    assert cache.get(user_id="u", run_id="r1", ref="contact:1") == Contact("Яна", {})
    assert cache.get(user_id="other", run_id="r1", ref="contact:1") is None
    now[0] = 11
    assert cache.get(user_id="u", run_id="r1", ref="contact:1") is None
    for run in ("a", "b", "c"):
        cache.put(user_id="u", run_id=run, contacts=[Contact(run, {})])
    assert cache.get(user_id="u", run_id="a", ref="contact:1") is None
    assert cache.get(user_id="u", run_id="c", ref="contact:1") == Contact("c", {})
