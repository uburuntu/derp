"""Contracts for versioned history persistence projections."""

from datetime import UTC, datetime

from aiogram.types import Chat, Message, PhotoSize, User

from derp.history.persistence import (
    CANONICAL_PROJECTION_SCHEMA_VERSION,
    HISTORY_DTO_SCHEMA_VERSION,
    attachment_references,
    project_persisted_message,
)
from derp.history.snapshot import (
    CaptureKind,
    MessageDirection,
    SnapshotRole,
    project_message_snapshot,
)


def _message(*, text: str | None = "hello", photo: bool = False) -> Message:
    return Message(
        message_id=9,
        date=datetime(2026, 7, 20, tzinfo=UTC),
        chat=Chat(id=-1001, type="supergroup", title="Room"),
        from_user=User(
            id=42,
            is_bot=False,
            first_name="Ada",
            username="ada",
        ),
        text=text,
        photo=(
            [
                PhotoSize(
                    file_id="volatile-id",
                    file_unique_id="stable-id",
                    width=640,
                    height=480,
                    file_size=123,
                )
            ]
            if photo
            else None
        ),
    )


def test_projects_three_versioned_representations() -> None:
    snapshot = project_message_snapshot(
        _message(photo=True),
        role=SnapshotRole.USER,
        direction=MessageDirection.INBOUND,
        capture=CaptureKind.AMBIENT,
    )

    projection = project_persisted_message(snapshot)

    assert projection.source_snapshot["schema_version"] == 1
    assert projection.source_snapshot["capture"] == "ambient"
    assert projection.history_dto == {
        "schema_version": HISTORY_DTO_SCHEMA_VERSION,
        "kind": "user_text",
        "source_message_id": 9,
        "timestamp": "2026-07-20T00:00:00Z",
        "speaker": {"id": 42, "display_name": "@ada"},
        "text": "hello",
        "attachments": [
            {
                "media_type": "photo",
                "file_id": "volatile-id",
                "file_unique_id": "stable-id",
            }
        ],
    }
    assert projection.canonical_projection == {
        "schema_version": CANONICAL_PROJECTION_SCHEMA_VERSION,
        "role": "user",
        "text": "hello",
        "attachment_types": ["photo"],
        "reply_to_message_id": None,
    }
    assert "volatile-id" not in repr(projection.canonical_projection)
    assert attachment_references(projection)[0].file_unique_id == "stable-id"


def test_media_only_message_gets_a_stable_text_marker() -> None:
    snapshot = project_message_snapshot(
        _message(text=None, photo=True),
        role=SnapshotRole.USER,
        direction=MessageDirection.INBOUND,
        capture=CaptureKind.EXPLICIT,
    )

    projection = project_persisted_message(snapshot)

    assert projection.text == "[photo]"
    assert projection.history_dto["text"] == "[photo]"
