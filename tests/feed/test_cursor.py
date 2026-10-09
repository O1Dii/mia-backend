"""The opaque keyset cursor (no database)."""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from app.modules.conversations.feed import FeedCursor, InvalidFeedCursorError

FEED = uuid.uuid4()


def raw(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


def test_round_trip() -> None:
    cursor = FeedCursor(FEED, 42)
    assert FeedCursor.decode(cursor.encode(), conversation_id=FEED) == cursor


def test_cursor_of_another_feed_is_rejected() -> None:
    with pytest.raises(InvalidFeedCursorError):
        FeedCursor.decode(FeedCursor(uuid.uuid4(), 5).encode(), conversation_id=FEED)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x" * 513,
        "not base64 !!",
        raw("string"),
        raw({"c": str(FEED), "s": 3}),
        raw({"c": str(FEED), "s": 3, "v": 2}),
        raw({"c": str(FEED), "s": 0, "v": 1}),
        raw({"c": str(FEED), "s": -4, "v": 1}),
        raw({"c": str(FEED), "s": True, "v": 1}),
        raw({"c": str(FEED), "s": "7", "v": 1}),
        raw({"c": str(FEED), "s": 3, "v": 1, "x": 1}),
    ],
)
def test_malformed_cursors_are_rejected(value: str) -> None:
    with pytest.raises(InvalidFeedCursorError):
        FeedCursor.decode(value, conversation_id=FEED)
