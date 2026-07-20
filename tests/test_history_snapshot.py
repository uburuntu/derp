"""Tests for normalized Telegram source snapshots."""

from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from aiogram.types import (
    Animation,
    Chat,
    Contact,
    Document,
    LivePhoto,
    Location,
    Message,
    MessageEntity,
    PaidMediaInfo,
    PaidMediaPhoto,
    PaidMediaVideo,
    PassportData,
    PhotoSize,
    SuccessfulPayment,
    User,
    Video,
    WebAppData,
)

from derp.history.snapshot import (
    TELEGRAM_SNAPSHOT_SCHEMA_VERSION,
    AttachmentMediaType,
    CaptureKind,
    EditKind,
    MediaGroupKind,
    MessageDirection,
    ReplyKind,
    SenderKind,
    SnapshotRole,
    TopicKind,
    project_message_snapshot,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
CHAT = Chat(id=-100123, type="supergroup", title="Test room", is_forum=True)
USER = User(
    id=42,
    is_bot=False,
    first_name="Ada",
    last_name="Lovelace",
    username="ada",
    language_code="en",
    is_premium=True,
)


def make_message(**values: object) -> Message:
    return Message(
        message_id=values.pop("message_id", 10),
        date=values.pop("date", NOW),
        chat=values.pop("chat", CHAT),
        from_user=values.pop("from_user", USER),
        **values,
    )


def project(message: Message):
    return project_message_snapshot(
        message,
        role=SnapshotRole.USER,
        direction=MessageDirection.INBOUND,
        capture=CaptureKind.EXPLICIT,
    )


def test_projects_explicit_message_classification_and_allowlisted_content() -> None:
    nested_reply = make_message(
        message_id=7,
        text="NESTED_REPLY_BODY_MUST_NOT_ESCAPE",
        reply_to_message=make_message(
            message_id=6,
            text="DEEP_REPLY_BODY_MUST_NOT_ESCAPE",
        ),
    )
    message = make_message(
        text="Hello, Ada",
        entities=[
            MessageEntity(
                type="text_mention",
                offset=7,
                length=3,
                user=User(
                    id=99,
                    is_bot=False,
                    first_name="MENTION_BODY_MUST_NOT_ESCAPE",
                ),
            )
        ],
        message_thread_id=55,
        is_topic_message=True,
        reply_to_message=nested_reply,
        edit_date=1_721_476_800,
        media_group_id="album-1",
    )

    snapshot = project(message)

    assert snapshot.schema_version == TELEGRAM_SNAPSHOT_SCHEMA_VERSION == 1
    assert snapshot.role is SnapshotRole.USER
    assert snapshot.direction is MessageDirection.INBOUND
    assert snapshot.capture is CaptureKind.EXPLICIT
    assert snapshot.topic.kind is TopicKind.THREAD
    assert snapshot.topic.topic_id == 55
    assert snapshot.reply.kind is ReplyKind.MESSAGE
    assert snapshot.reply.message_id == 7
    assert snapshot.edit.kind is EditKind.EDITED
    assert snapshot.edit.edited_at == datetime.fromtimestamp(1_721_476_800, UTC)
    assert snapshot.media_group.kind is MediaGroupKind.GROUPED
    assert snapshot.media_group.media_group_id == "album-1"
    assert snapshot.sender is not None
    assert snapshot.sender.kind is SenderKind.USER
    assert snapshot.sender.id == USER.id
    assert snapshot.sender.first_name == "Ada"
    assert snapshot.text == "Hello, Ada"
    assert snapshot.caption is None
    assert snapshot.entities[0].user_id == 99
    serialized = repr(asdict(snapshot))
    assert "NESTED_REPLY_BODY_MUST_NOT_ESCAPE" not in serialized
    assert "DEEP_REPLY_BODY_MUST_NOT_ESCAPE" not in serialized
    assert "MENTION_BODY_MUST_NOT_ESCAPE" not in serialized


def test_sender_chat_takes_precedence_over_telegram_fake_sender() -> None:
    sender_chat = Chat(
        id=-100999,
        type="channel",
        title="Announcements",
        username="announcements",
    )

    snapshot = project(make_message(sender_chat=sender_chat))

    assert snapshot.sender is not None
    assert snapshot.sender.kind is SenderKind.CHAT
    assert snapshot.sender.id == sender_chat.id
    assert snapshot.sender.title == "Announcements"
    assert snapshot.sender.username == "announcements"
    assert snapshot.sender.first_name is None


def test_projects_largest_photo_as_one_stable_attachment() -> None:
    message = make_message(
        caption="A photo",
        caption_entities=[MessageEntity(type="italic", offset=2, length=5)],
        photo=[
            PhotoSize(
                file_id="small-file",
                file_unique_id="small-unique",
                width=90,
                height=90,
                file_size=100,
            ),
            PhotoSize(
                file_id="large-file",
                file_unique_id="large-unique",
                width=1280,
                height=720,
                file_size=5_000,
            ),
        ],
    )

    snapshot = project(message)

    assert snapshot.caption == "A photo"
    assert snapshot.caption_entities[0].type == "italic"
    assert len(snapshot.attachments) == 1
    attachment = snapshot.attachments[0]
    assert attachment.media_type is AttachmentMediaType.PHOTO
    assert attachment.file_id == "large-file"
    assert attachment.file_unique_id == "large-unique"
    assert attachment.size == 5_000
    assert (attachment.width, attachment.height) == (1280, 720)


def test_projects_multiple_paid_media_references_without_price_body() -> None:
    message = make_message(
        paid_media=PaidMediaInfo(
            star_count=987_654,
            paid_media=[
                PaidMediaPhoto(
                    photo=[
                        PhotoSize(
                            file_id="photo-file",
                            file_unique_id="photo-unique",
                            width=800,
                            height=600,
                            file_size=123,
                        )
                    ]
                ),
                PaidMediaVideo(
                    video=Video(
                        file_id="video-file",
                        file_unique_id="video-unique",
                        width=1920,
                        height=1080,
                        duration=12,
                        file_name="clip.mp4",
                        mime_type="video/mp4",
                        file_size=456,
                    )
                ),
            ],
        )
    )

    snapshot = project(message)

    assert [item.media_type for item in snapshot.attachments] == [
        AttachmentMediaType.PHOTO,
        AttachmentMediaType.VIDEO,
    ]
    assert snapshot.attachments[1].filename == "clip.mp4"
    assert snapshot.attachments[1].mime_type == "video/mp4"
    assert snapshot.attachments[1].duration == 12
    assert "987654" not in repr(asdict(snapshot))


def test_live_photo_keeps_video_and_static_photo_references() -> None:
    still = PhotoSize(
        file_id="still-file",
        file_unique_id="still-unique",
        width=1200,
        height=900,
        file_size=300,
    )
    message = make_message(
        live_photo=LivePhoto(
            file_id="motion-file",
            file_unique_id="motion-unique",
            width=1200,
            height=900,
            duration=3,
            photo=[still],
            mime_type="video/mp4",
            file_size=1_000,
        ),
        photo=[still],
    )

    snapshot = project(message)

    assert [item.media_type for item in snapshot.attachments] == [
        AttachmentMediaType.LIVE_PHOTO,
        AttachmentMediaType.PHOTO,
    ]
    assert [item.file_id for item in snapshot.attachments] == [
        "motion-file",
        "still-file",
    ]


def test_animation_does_not_duplicate_telegram_compatibility_document() -> None:
    message = make_message(
        animation=Animation(
            file_id="animation-file",
            file_unique_id="animation-unique",
            width=640,
            height=480,
            duration=2,
            file_name="animation.mp4",
            mime_type="video/mp4",
            file_size=800,
        ),
        document=Document(
            file_id="compat-document-file",
            file_unique_id="compat-document-unique",
            file_name="SHOULD_NOT_BE_DUPLICATED",
        ),
    )

    snapshot = project(message)

    assert len(snapshot.attachments) == 1
    assert snapshot.attachments[0].media_type is AttachmentMediaType.ANIMATION
    assert snapshot.attachments[0].file_id == "animation-file"
    assert "SHOULD_NOT_BE_DUPLICATED" not in repr(asdict(snapshot))


@pytest.mark.parametrize(
    ("sensitive_field", "secret"),
    [
        (
            {
                "successful_payment": SuccessfulPayment(
                    currency="XTR",
                    total_amount=10,
                    invoice_payload="PAYMENT_PAYLOAD_SECRET",
                    telegram_payment_charge_id="PAYMENT_TELEGRAM_SECRET",
                    provider_payment_charge_id="PAYMENT_PROVIDER_SECRET",
                )
            },
            "PAYMENT_PAYLOAD_SECRET",
        ),
        (
            {
                "passport_data": PassportData.model_construct(
                    data=[{"value": "PASSPORT_BODY_SECRET"}],
                    credentials={"value": "PASSPORT_CREDENTIAL_SECRET"},
                )
            },
            "PASSPORT_BODY_SECRET",
        ),
        ({"connected_website": "AUTH_BODY_SECRET"}, "AUTH_BODY_SECRET"),
        (
            {
                "contact": Contact(
                    phone_number="CONTACT_BODY_SECRET",
                    first_name="Private",
                )
            },
            "CONTACT_BODY_SECRET",
        ),
        (
            {
                "location": Location(
                    latitude=51.501_364,
                    longitude=-0.141_89,
                    address="LOCATION_BODY_SECRET",
                )
            },
            "LOCATION_BODY_SECRET",
        ),
        (
            {
                "web_app_data": WebAppData(
                    data="WEB_APP_BODY_SECRET",
                    button_text="Open",
                )
            },
            "WEB_APP_BODY_SECRET",
        ),
    ],
)
def test_sensitive_telegram_bodies_are_never_copied(
    sensitive_field: dict[str, object],
    secret: str,
) -> None:
    snapshot = project(make_message(**sensitive_field))

    assert secret not in repr(asdict(snapshot))


def test_attachment_projection_never_copies_signed_download_url() -> None:
    signed_url = "https://api.telegram.org/file/botSECRET_TOKEN/document.pdf?signature=SECRET"
    document = Document(
        file_id="document-file",
        file_unique_id="document-unique",
        file_name="document.pdf",
        mime_type="application/pdf",
        file_size=12_345,
        signed_url=signed_url,
    )

    snapshot = project(make_message(document=document))

    assert snapshot.attachments[0].file_id == "document-file"
    assert snapshot.attachments[0].file_unique_id == "document-unique"
    assert signed_url not in repr(asdict(snapshot))
