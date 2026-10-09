"""speech/display parsing in both model modes; every result satisfies the contract."""

from __future__ import annotations

import json

import pytest
from app.modules.runs.response import clip, parse_response, to_plain

from tests.fakes import assert_mvp_valid


def parse(text: str, *, structured: bool, limit: int = 300) -> dict[str, object]:
    parsed = parse_response(text, structured=structured, speech_max_chars=limit)
    assert_mvp_valid(parsed.response, "assistant-response")
    return parsed.response


def test_structured_answer_is_kept() -> None:
    body = {
        "speech": "Сегодня три встречи, первая в 10:00 с Олегом.",
        "display": {"kind": "card", "title": "Сегодня", "markdown": "- 10:00 Олег"},
        "followups": ["Перенести просроченные на завтра?"],
    }
    assert parse(json.dumps(body, ensure_ascii=False), structured=True) == body


def test_structured_answer_in_code_fence_with_prose() -> None:
    fenced = '```json\n{"speech": "Готово.", "display": null, "followups": []}\n```'
    assert parse(fenced, structured=True)["speech"] == "Готово."
    wrapped = 'Вот ответ: {"speech": "Ок.", "display": "**жирно**"} конец'
    result = parse(wrapped, structured=True)
    assert result["display"] == {"kind": "text", "markdown": "**жирно**"}


def test_structured_speech_is_sanitized_and_original_kept_on_screen() -> None:
    raw = "Смотри **тут**: [ссылка](https://example.com/a) и https://x.test\nвторая строка"
    result = parse(json.dumps({"speech": raw, "display": None}), structured=True)
    assert result["speech"] == "Смотри тут: ссылка и вторая строка"
    assert result["display"] == {"kind": "text", "markdown": raw}


def test_structured_invalid_display_and_followups_are_normalized() -> None:
    body = {
        "speech": "",
        "display": {"kind": "app", "plugin_id": "BAD", "resource_uri": "http://no", "markdown": ""},
        "followups": ["  раз  ", "", 3, "два", "три", "четыре"],
    }
    result = parse(json.dumps(body), structured=True)
    assert result["display"] is None
    assert result["speech"]  # fallback, never empty
    assert result["followups"] == ["раз", "два", "три"]

    app = {
        "kind": "app",
        "plugin_id": "app.mindweaver",
        "resource_uri": "ui://mindweaver/map",
        "structured_content": {"id": 1},
    }
    link = {"kind": "link", "url": "https://example.com", "title": "Пример"}
    assert (
        parse(json.dumps({"speech": "Открыла.", "display": app}), structured=True)["display"] == app
    )
    assert (
        parse(json.dumps({"speech": "Вот.", "display": link}), structured=True)["display"] == link
    )


def test_structured_speech_falls_back_to_display_first_sentence() -> None:
    body = {"speech": None, "display": {"kind": "text", "markdown": "# Итог\nВсё готово. Ещё."}}
    assert parse(json.dumps(body), structured=True)["speech"] == "Итог"


def test_plain_first_sentence_is_speech_and_full_text_is_display() -> None:
    text = "Сегодня три встречи. Первая в 10:00 с Олегом.\n\n- Олег\n- Яна"
    result = parse(text, structured=False)
    assert result == {
        "speech": "Сегодня три встречи.",
        "display": {"kind": "text", "markdown": text},
        "followups": [],
    }


def test_plain_single_sentence_needs_no_display() -> None:
    assert parse("Готово, таймер на пять минут.", structured=False) == {
        "speech": "Готово, таймер на пять минут.",
        "display": None,
        "followups": [],
    }


def test_plain_markdown_heading_and_list_markers_are_not_spoken() -> None:
    result = parse("## Погода\n1. Солнечно\n2. Ветер", structured=False)
    assert result["speech"] == "Погода"
    numbered = parse("1. Купить хлеб. 2. Позвонить.", structured=False)
    assert numbered["speech"] == "Купить хлеб."


def test_unparsable_structured_output_uses_heuristic() -> None:
    parsed = parse_response("Просто текст. Второе.", structured=True)
    assert parsed.mode == "heuristic_fallback"
    assert parsed.response["speech"] == "Просто текст."


@pytest.mark.parametrize("limit", [250, 300, 60])
def test_voice_speech_is_length_limited(limit: int) -> None:
    long_sentence = "Это очень длинное предложение " * 30 + "."
    for structured, text in [
        (False, long_sentence),
        (True, json.dumps({"speech": long_sentence, "display": None})),
    ]:
        result = parse(text, structured=structured, limit=limit)
        assert len(str(result["speech"])) <= limit
        assert result["display"] is not None


def test_empty_answer_has_fallback_speech() -> None:
    assert parse("", structured=False)["speech"]
    assert parse("   ", structured=True)["display"] is None


def test_clip_prefers_sentence_end() -> None:
    assert clip("Первое предложение. Второе предложение длиннее.", 30) == "Первое предложение."
    assert clip("слово " * 20, 20).endswith("…")
    assert len(clip("x" * 50, 10)) <= 10


def test_to_plain_strips_markdown() -> None:
    assert to_plain("**a** `b` [c](https://d) > e\n```\ncode\n```\n| f |") == "a b c e\nf"


def test_feed_text_prefers_display() -> None:
    parsed = parse_response(
        json.dumps(
            {
                "speech": "Вот ссылка.",
                "display": {"kind": "link", "url": "https://a.b", "title": "A"},
            }
        ),
        structured=True,
    )
    assert parsed.feed_text == "Вот ссылка.\n\n[A](https://a.b)"
    assert parse_response("Коротко.", structured=False).feed_text == "Коротко."
