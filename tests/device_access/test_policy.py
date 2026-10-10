"""ADR-0016 policy matrix, capability vocabulary and data minimization (M70)."""

from __future__ import annotations

import pytest
from app.modules.tools.device_policy import (
    CAPABILITIES,
    PHONE_PLACEHOLDER,
    Requester,
    allowed_scopes,
    capability_for,
    decide,
    minimize_device_result,
    scrub_phone_numbers,
)

PHONE = "phone-1"
WEB = Requester("web", "браузер Chrome на macOS", web_session_id="s-1")
WEBVIEW = Requester("device_webview", "Mia на телефоне", device_id=PHONE, web_session_id="s-2")
APP = Requester("device_app", "Mia на телефоне", device_id=PHONE)
OTHER_PHONE = Requester("device_app", "Mia на планшете", device_id="tablet-1")
UNKNOWN = Requester("unknown", "другой клиент Mia")
CONTACTS = CAPABILITIES["contacts.read"]
TELEGRAM = CAPABILITIES["telegram.draft"]


def test_tools_map_to_capabilities_and_unknown_tools_are_strictest() -> None:
    assert capability_for("device.contacts.search").name == "contacts.read"
    assert capability_for("device.alarm.set").kind == "action"
    assert capability_for("telegram.handoff").kind == "external"
    unknown = capability_for("device.calendar.read")
    assert unknown.name == "device.calendar.read" and unknown.kind == "external"
    assert not unknown.persistent_allowed and not unknown.run_scoped


@pytest.mark.parametrize("requester", [APP, WEBVIEW])
def test_the_target_device_itself_is_allowed(requester: Requester) -> None:
    decision = decide(CONTACTS, requester, PHONE, remote="deny")
    assert (decision.value, decision.reason) == ("allow", "same_device")


@pytest.mark.parametrize("requester", [WEB, OTHER_PHONE, UNKNOWN])
def test_other_clients_must_ask_on_the_device(requester: Requester) -> None:
    assert decide(CONTACTS, requester, PHONE).value == "ask"


def test_jobs_and_apps_are_refused_not_asked() -> None:
    for kind in ("job", "app"):
        decision = decide(CONTACTS, Requester(kind, "x"), PHONE)
        assert (decision.value, decision.reason) == ("deny", "requester_not_allowed")


def test_disabled_capability_is_denied_even_with_grants() -> None:
    decision = decide(CONTACTS, WEB, PHONE, remote="deny", persistent_grant_id="g")
    assert (decision.value, decision.reason) == ("deny", "capability_disabled")


def test_grants_allow_but_never_for_external_effects() -> None:
    assert decide(CONTACTS, WEB, PHONE, run_grant_id="r").reason == "run_grant"
    assert decide(CONTACTS, WEB, PHONE, persistent_grant_id="p").reason == "persistent_grant"
    assert decide(TELEGRAM, WEB, PHONE, run_grant_id="r", persistent_grant_id="p").value == "ask"
    # A requester without a stable identity cannot hold a persistent grant.
    assert decide(CONTACTS, UNKNOWN, PHONE, persistent_grant_id="p").value == "ask"


def test_allowed_scopes() -> None:
    assert allowed_scopes(CONTACTS, WEB) == ["run", "persistent"]
    assert allowed_scopes(CONTACTS, UNKNOWN) == ["run"]
    assert allowed_scopes(TELEGRAM, WEB) == ["run"]
    assert WEB.key == "web:s-1" and WEBVIEW.key == f"device:{PHONE}" and UNKNOWN.key is None


@pytest.mark.parametrize(
    "number",
    ["+375 29 123-45-67", "+375291234567", "+7 (912) 345-67-89", "tel:+375291234567"],
)
def test_international_numbers_are_always_scrubbed(number: str) -> None:
    scrubbed = scrub_phone_numbers(f"Позвонить {number} вечером")
    assert PHONE_PLACEHOLDER in scrubbed and "1234" not in scrubbed.replace("—", "")


def test_dates_and_times_survive_the_light_scrub() -> None:
    text = "Будильник на 2026-10-10T07:00:00+03:00, id 12, в 07:30"
    assert scrub_phone_numbers(text) == text


def test_contact_results_keep_names_and_counts_only() -> None:
    content, structured = minimize_device_result(
        "device.contacts.search",
        "Found 2 contacts: Яна Петрова 8 029 123-45-67; Яна (работа) +375291112233",
        {
            "contacts": [
                {"display_name": "Яна Петрова", "phones": ["+375291234567", "80291234567"]},
                {"name": "Яна (работа)", "phone_count": 1, "email": "yana@example.test"},
                {"name": "+375 29 765-43-21"},
            ]
        },
    )
    assert "8 029" not in content and "375" not in content
    assert structured == {
        "matches": [
            {"name": "Яна Петрова", "phone_count": 2},
            {"name": "Яна (работа)", "phone_count": 1},
            {"name": PHONE_PLACEHOLDER},
        ]
    }


def test_other_device_results_lose_numbers_in_nested_data() -> None:
    _, structured = minimize_device_result(
        "device.app.open", "ok", {"opened": True, "debug": {"caller": "+375291234567"}}
    )
    assert structured == {"opened": True, "debug": {"caller": PHONE_PLACEHOLDER}}
