import base64
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from app.modules.conversations.cursors import (
    ConversationCursor,
    EntryCursor,
    InvalidCursorError,
)


def test_conversation_cursor_round_trip_is_opaque_and_versioned() -> None:
    cursor = ConversationCursor(
        updated_at=datetime(2026, 9, 10, 12, 30, tzinfo=UTC),
        conversation_id=UUID("10000000-0000-4000-8000-000000000001"),
    )

    encoded = cursor.encode()

    assert ConversationCursor.decode(encoded) == cursor
    assert "conversations" not in encoded


def test_entry_cursor_rejects_wrong_feed_and_boolean_sequence() -> None:
    payload = json.dumps({"kind": "entries", "sequence": True, "v": 1}).encode()
    malformed = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()

    with pytest.raises(InvalidCursorError, match="sequence"):
        EntryCursor.decode(malformed)

    with pytest.raises(InvalidCursorError, match="shape"):
        EntryCursor.decode(
            ConversationCursor(
                updated_at=datetime.now(UTC),
                conversation_id=UUID("10000000-0000-4000-8000-000000000001"),
            ).encode()
        )


@pytest.mark.parametrize("cursor", ["", "not*base64", "a" * 513])
def test_invalid_cursor_input_is_normalized(cursor: str) -> None:
    with pytest.raises(InvalidCursorError):
        EntryCursor.decode(cursor)
