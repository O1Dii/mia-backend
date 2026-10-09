"""Final answer contract ``{speech, display, followups}`` (ADR-0011 §2, Review §5.2(b)).

Two model modes:

- structured output: the model returns the JSON object; it is normalized so the
  result always satisfies ``assistant-response.schema.json``;
- plain text (or unparsable JSON): heuristic "first sentence = speech", the full
  text goes to ``display`` when it says more than the speech.

``speech`` is always plain (no markdown, links, line breaks) and clipped to the
profile limit, which is at most the contract's hard limit of 300 characters.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.modules.runs.profiles import SPEECH_HARD_LIMIT

ResponseMode = Literal["structured", "heuristic", "heuristic_fallback"]

FALLBACK_SPEECH_WITH_DISPLAY = "Подробности на экране."
FALLBACK_SPEECH = "Не получилось ответить, попробуйте ещё раз."
FOLLOWUP_MAX_CHARS = 120
MAX_FOLLOWUPS = 3
DISPLAY_MAX_CHARS = 32000
TITLE_MAX_CHARS = 120

_CODE_FENCE = re.compile(r"```.*?(?:```|$)", re.S)
_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S | re.I)
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S*", re.I)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_QUOTE = re.compile(r"^\s*>+\s?", re.M)
_LIST_MARK = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+", re.M)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}.*$", re.M)
_EMPHASIS = re.compile(r"[*`#]")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_FORBIDDEN = re.compile(r"[|\[\]<>]")
_SENTENCE_END = re.compile(r"[.!?…]+[\"»”')]*(?=\s|$)")
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}(\.[a-z][a-z0-9-]{1,47}){1,2}$")
_UI_URI = re.compile(r"^ui://[a-z0-9.-]+(/[A-Za-z0-9._~-]+)*$")
_HTTPS = re.compile(r"^https://[^\s]+$")


@dataclass(frozen=True, slots=True)
class ParsedResponse:
    response: dict[str, Any]
    mode: ResponseMode

    @property
    def feed_text(self) -> str:
        """What the feed shows: the display markdown when there is one, else speech."""
        display = self.response["display"]
        if isinstance(display, dict) and isinstance(display.get("markdown"), str):
            return str(display["markdown"])
        if isinstance(display, dict) and display.get("kind") == "link":
            return f"{self.response['speech']}\n\n[{display['title']}]({display['url']})"
        return str(self.response["speech"])


def to_plain(markdown: str) -> str:
    """Markdown → speakable text; keeps line breaks between blocks."""
    text = _CODE_FENCE.sub(" ", markdown)
    text = _MD_IMAGE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _TABLE_RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _LIST_MARK.sub("", text)
    text = _HTML_TAG.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = _FORBIDDEN.sub(" ", text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def clip(text: str, limit: int) -> str:
    """Clip at a sentence end, else at a word boundary with an ellipsis."""
    if len(text) <= limit:
        return text
    window = text[: limit - 1]
    ends = [match.end() for match in _SENTENCE_END.finditer(window)]
    if ends and ends[-1] >= limit // 2:
        return window[: ends[-1]].rstrip()
    space = window.rfind(" ")
    cut = window[:space] if space >= limit // 2 else window
    return cut.rstrip(" ,;:—-") + "…"


def first_sentence(plain: str) -> str:
    line = next((line for line in plain.splitlines() if line.strip()), "")
    match = _SENTENCE_END.search(line)
    return line[: match.end()].strip() if match else line.strip()


def speech_from(text: str, limit: int) -> str:
    """Plain single-line speech within ``limit`` (may be empty)."""
    single = " ".join(to_plain(text).split())
    return clip(single, limit)


def _string(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped[:limit] if stripped else None


def normalize_display(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        markdown = _string(value, DISPLAY_MAX_CHARS)
        return {"kind": "text", "markdown": markdown} if markdown else None
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    markdown = _string(value.get("markdown"), DISPLAY_MAX_CHARS)
    if kind == "card" and markdown:
        card: dict[str, Any] = {"kind": "card", "markdown": markdown}
        title = _string(value.get("title"), TITLE_MAX_CHARS)
        if title:
            card["title"] = title
        source = _string(value.get("source_tool_call_id"), 128)
        if source:
            card["source_tool_call_id"] = source
        return card
    if kind == "app":
        plugin_id = value.get("plugin_id")
        uri = value.get("resource_uri")
        if (
            isinstance(plugin_id, str)
            and _PLUGIN_ID.match(plugin_id)
            and isinstance(uri, str)
            and len(uri) <= 512
            and _UI_URI.match(uri)
        ):
            app: dict[str, Any] = {"kind": "app", "plugin_id": plugin_id, "resource_uri": uri}
            if isinstance(value.get("structured_content"), dict):
                app["structured_content"] = value["structured_content"]
            fallback = _string(value.get("fallback_markdown"), DISPLAY_MAX_CHARS)
            if fallback:
                app["fallback_markdown"] = fallback
            return app
    if kind == "link":
        url = value.get("url")
        title = _string(value.get("title"), TITLE_MAX_CHARS)
        if isinstance(url, str) and len(url) <= 2048 and _HTTPS.match(url) and title:
            return {"kind": "link", "url": url, "title": title}
    return {"kind": "text", "markdown": markdown} if markdown else None


def normalize_followups(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    followups: list[str] = []
    for item in value:
        text = _string(item, 10 * FOLLOWUP_MAX_CHARS)
        if text:
            followups.append(clip(" ".join(text.split()), FOLLOWUP_MAX_CHARS))
        if len(followups) == MAX_FOLLOWUPS:
            break
    return followups


def _finish(speech: str, display: dict[str, Any] | None, followups: list[str]) -> dict[str, Any]:
    if not speech:
        speech = FALLBACK_SPEECH_WITH_DISPLAY if display is not None else FALLBACK_SPEECH
    return {"speech": speech, "display": display, "followups": followups}


def _json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    fenced = _JSON_FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1)
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        candidate = candidate[start : end + 1]
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def parse_structured(text: str, *, speech_max_chars: int) -> dict[str, Any] | None:
    data = _json_object(text)
    if data is None or not ({"speech", "display"} & data.keys()):
        return None
    display = normalize_display(data.get("display"))
    raw_speech = data.get("speech") if isinstance(data.get("speech"), str) else ""
    assert isinstance(raw_speech, str)
    speech = speech_from(raw_speech, speech_max_chars)
    if not speech and display is not None and isinstance(display.get("markdown"), str):
        speech = clip(first_sentence(to_plain(display["markdown"])), speech_max_chars)
    original = " ".join(raw_speech.split())
    if display is None and original and speech != original:
        display = {"kind": "text", "markdown": raw_speech.strip()[:DISPLAY_MAX_CHARS]}
    return _finish(speech, display, normalize_followups(data.get("followups")))


def parse_plain(text: str, *, speech_max_chars: int) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return _finish("", None, [])
    plain = to_plain(stripped)
    speech = clip(first_sentence(plain), speech_max_chars)
    flattened = " ".join(plain.split())
    says_more = flattened != speech or stripped != flattened
    display = {"kind": "text", "markdown": stripped[:DISPLAY_MAX_CHARS]} if says_more else None
    return _finish(speech, display, [])


def parse_response(
    text: str, *, structured: bool, speech_max_chars: int = SPEECH_HARD_LIMIT
) -> ParsedResponse:
    limit = min(speech_max_chars, SPEECH_HARD_LIMIT)
    if structured:
        parsed = parse_structured(text, speech_max_chars=limit)
        if parsed is not None:
            return ParsedResponse(parsed, "structured")
        return ParsedResponse(parse_plain(text, speech_max_chars=limit), "heuristic_fallback")
    return ParsedResponse(parse_plain(text, speech_max_chars=limit), "heuristic")
