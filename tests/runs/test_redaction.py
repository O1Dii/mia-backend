"""Tool-call journal material (M73): redacted arguments and the failure reason."""

from __future__ import annotations

import json

from app.modules.runs.redaction import (
    MASK,
    MAX_ARGUMENTS_BYTES,
    failure_reason,
    redact_arguments,
    scrub_text,
)


def test_plain_arguments_pass_through() -> None:
    arguments = {"query": "главные новости", "limit": 5, "fresh": True, "tags": ["a", "b"]}
    assert redact_arguments(arguments) == arguments


def test_secrets_emails_phones_and_tokens_are_masked() -> None:
    redacted = redact_arguments(
        {
            "query": "почта ivan@example.com или +7 (999) 123-45-67",
            "api_key": "sk-live-1234",
            "nested": {"Authorization": "Bearer abc", "note": "ok"},
            "blob": "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        }
    )
    assert redacted["query"] == "почта [email] или [телефон]"
    assert redacted["api_key"] == MASK
    assert redacted["nested"] == {"Authorization": MASK, "note": "ok"}
    assert redacted["blob"] == MASK


def test_device_arguments_keep_only_their_shape() -> None:
    redacted = redact_arguments({"name": "Яна Петрова", "limit": 3}, device=True)
    assert redacted == {"name": MASK, "limit": 3}


def test_arguments_are_bounded() -> None:
    huge = {f"k{i}": "x" * 290 for i in range(15)}
    redacted = redact_arguments(huge)
    assert len(json.dumps(redacted, ensure_ascii=False).encode()) <= MAX_ARGUMENTS_BYTES + 200
    assert redacted["_truncated"] is True
    deep = {"a": {"b": {"c": {"d": {"e": "deep"}}}}}
    assert redact_arguments(deep) == {"a": {"b": {"c": {"d": MASK}}}}
    assert len(redact_arguments({"s": "я" * 1000})["s"]) == 300


def test_failure_reason_strips_the_model_frame_and_personal_data() -> None:
    framed = (
        '<tool_result tool="builtin.news.refresh" trust="untrusted">\n'
        "По запросу «новости» публикаций не найдено, почта a@b.io\n"
        "</tool_result>"
    )
    assert failure_reason(framed) == "По запросу «новости» публикаций не найдено, почта [email]"
    assert failure_reason("") is None
    assert failure_reason('<tool_result tool="x" trust="untrusted">\n\n</tool_result>') is None
    assert len(failure_reason("я" * 1000) or "") == 300


def test_scrub_text_leaves_ordinary_numbers_alone() -> None:
    assert scrub_text("за 2 дня, 48 часов, в 2026 году") == "за 2 дня, 48 часов, в 2026 году"
