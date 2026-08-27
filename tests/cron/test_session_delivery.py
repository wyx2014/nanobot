import pytest

from nanobot.cron.session_delivery import (
    origin_delivery_context,
    should_deliver_direct_reminder,
)
from nanobot.cron.types import CronJob, CronPayload


def test_origin_delivery_context_uses_explicit_origin_fields() -> None:
    metadata = {
        "context_chat_id": "456",
        "parent_channel_id": "456",
        "thread_id": "777",
    }
    job = CronJob(
        id="thread-check",
        name="Thread check",
        payload=CronPayload(
            message="check",
            session_key="discord:456:thread:777",
            origin_channel="discord",
            origin_chat_id="777",
            origin_metadata=metadata,
        ),
    )

    channel, chat_id, returned_metadata = origin_delivery_context(job)

    assert channel == "discord"
    assert chat_id == "777"
    assert returned_metadata == metadata
    assert returned_metadata is not metadata


def test_origin_delivery_context_rejects_missing_origin_fields() -> None:
    job = CronJob(
        id="old-bound",
        name="Old bound job",
        payload=CronPayload(
            message="check",
            session_key="websocket:chat-1",
        ),
    )

    with pytest.raises(ValueError, match="missing origin delivery context"):
        origin_delivery_context(job)


def test_websocket_reminder_uses_desktop_notification_instead_of_chat_delivery() -> None:
    job = CronJob(
        id="desktop-reminder",
        name="Desktop reminder",
        payload=CronPayload(origin_channel="websocket"),
    )

    assert should_deliver_direct_reminder(job) is False


@pytest.mark.parametrize("channel", ["telegram", "discord", "feishu"])
def test_external_reminder_is_delivered_to_origin_channel(channel: str) -> None:
    job = CronJob(
        id=f"{channel}-reminder",
        name="External reminder",
        payload=CronPayload(origin_channel=channel),
    )

    assert should_deliver_direct_reminder(job) is True
