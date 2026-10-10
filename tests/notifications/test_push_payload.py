"""OI-M71-001: a fired cloud alarm or timer reaches the app as a data-only push."""

import uuid

import pytest
from app.modules.notifications.channels import OutboundMessage, PushChannel


def message(source_type: str, source_id: str) -> OutboundMessage:
    return OutboundMessage(
        delivery_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        inbox_item_id=uuid.uuid4(),
        source_type=source_type,
        source_id=source_id,
        title="Будильник",
        text="Пора вставать.",
        response={},
        attempt=1,
    )


@pytest.mark.parametrize("source_id", ["builtin.alarm:a1", "builtin.timer:t1"])
def test_alarm_and_timer_are_data_only_with_a_short_ttl(source_id: str) -> None:
    payload = PushChannel.payload(message("plugin", source_id))
    assert payload.data_only
    assert payload.ttl_seconds == 600
    assert payload.data["source_id"] == source_id


@pytest.mark.parametrize(
    ("source_type", "source_id"), [("plugin", "builtin.notes:n1"), ("job", "builtin.alarm:x")]
)
def test_other_items_keep_the_notification_block(source_type: str, source_id: str) -> None:
    payload = PushChannel.payload(message(source_type, source_id))
    assert not payload.data_only
    assert payload.ttl_seconds == 86400
