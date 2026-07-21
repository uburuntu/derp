"""Delivery targets and Telegram send certainty stay explicit."""

import asyncio
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from derp.artifacts import ArtifactKind, ArtifactTooLargeError
from derp.delivery import (
    MAX_TELEGRAM_ALBUM_ITEMS,
    MAX_TELEGRAM_FILE_BYTES,
    MAX_TELEGRAM_PHOTO_BYTES,
    Delivered,
    DeliveryBatchKind,
    DeliveryFailed,
    DeliveryMedia,
    DeliveryTarget,
    DeliveryUncertain,
    TelegramMediaKind,
    classify_delivery_batch,
    classify_delivery_exception,
)


def test_delivery_target_preserves_topic_reply_and_business_scope() -> None:
    target = DeliveryTarget(-1001, 77, 42, "business")

    assert target.thread_id == 77
    assert target.reply_to_message_id == 42
    assert target.business_connection_id == "business"


@pytest.mark.parametrize(
    "target",
    [
        DeliveryTarget,
        lambda chat_id, *_: DeliveryTarget(chat_id, 0, None),
        lambda chat_id, *_: DeliveryTarget(chat_id, None, 0),
        lambda chat_id, *_: DeliveryTarget(chat_id, None, None, " "),
    ],
)
def test_delivery_target_rejects_invalid_coordinates(target) -> None:
    with pytest.raises(ValueError):
        target(0, None, None)


def test_network_and_unknown_failures_are_never_blindly_retried() -> None:
    method = MagicMock()

    network = classify_delivery_exception(TelegramNetworkError(method, "timeout"))
    timeout = classify_delivery_exception(TimeoutError())
    server = classify_delivery_exception(TelegramServerError(method, "unavailable"))
    cancellation = classify_delivery_exception(asyncio.CancelledError())
    unknown = classify_delivery_exception(RuntimeError("unknown send state"))

    assert isinstance(network, DeliveryUncertain)
    assert isinstance(timeout, DeliveryUncertain)
    assert isinstance(server, DeliveryUncertain)
    assert isinstance(cancellation, DeliveryUncertain)
    assert isinstance(unknown, DeliveryUncertain)


def test_server_rejections_distinguish_retryable_flood_control() -> None:
    method = MagicMock()

    retry = classify_delivery_exception(TelegramRetryAfter(method, "wait", 5))
    bad_request = classify_delivery_exception(TelegramBadRequest(method, "bad"))

    assert retry == DeliveryFailed("TelegramRetryAfter", retryable=True)
    assert bad_request == DeliveryFailed("TelegramBadRequest", retryable=False)


def test_delivered_requires_positive_acknowledged_message_ids() -> None:
    assert Delivered((1, 2)).message_ids == (1, 2)
    with pytest.raises(ValueError):
        Delivered(())


@pytest.mark.parametrize(
    ("kind", "mime_type", "artifact_kind"),
    [
        (TelegramMediaKind.PHOTO, "image/png", ArtifactKind.IMAGE),
        (TelegramMediaKind.VIDEO, "video/mp4", ArtifactKind.VIDEO),
        (TelegramMediaKind.AUDIO, "audio/mpeg", ArtifactKind.AUDIO),
        (TelegramMediaKind.VOICE, "audio/ogg", ArtifactKind.VOICE),
        (
            TelegramMediaKind.DOCUMENT,
            "application/pdf",
            ArtifactKind.DOCUMENT,
        ),
    ],
)
def test_delivery_media_preserves_explicit_telegram_presentation(
    kind: TelegramMediaKind,
    mime_type: str,
    artifact_kind: ArtifactKind,
) -> None:
    media = DeliveryMedia(kind, mime_type.upper(), b"payload")

    assert media.mime_type == mime_type
    assert media.artifact_kind is artifact_kind
    assert TelegramMediaKind.from_artifact_kind(artifact_kind) is kind


@pytest.mark.parametrize(
    ("kind", "mime_type"),
    [
        (TelegramMediaKind.PHOTO, "image/gif"),
        (TelegramMediaKind.VIDEO, "video/webm"),
        (TelegramMediaKind.AUDIO, "audio/ogg"),
        (TelegramMediaKind.VOICE, "audio/wav"),
    ],
)
def test_delivery_media_rejects_mime_types_telegram_will_not_present(
    kind: TelegramMediaKind,
    mime_type: str,
) -> None:
    with pytest.raises(ValueError, match="MIME type"):
        DeliveryMedia(kind, mime_type, b"payload")


def test_document_delivery_rejects_malformed_mime_but_accepts_general_files() -> None:
    assert (
        DeliveryMedia(
            TelegramMediaKind.DOCUMENT,
            "text/plain",
            b"document",
        ).mime_type
        == "text/plain"
    )
    with pytest.raises(ValueError, match="mime_type"):
        DeliveryMedia(TelegramMediaKind.DOCUMENT, "not-a-mime", b"document")


@pytest.mark.parametrize(
    ("kind", "limit"),
    [
        (TelegramMediaKind.PHOTO, MAX_TELEGRAM_PHOTO_BYTES),
        (TelegramMediaKind.VIDEO, MAX_TELEGRAM_FILE_BYTES),
        (TelegramMediaKind.AUDIO, MAX_TELEGRAM_FILE_BYTES),
        (TelegramMediaKind.VOICE, MAX_TELEGRAM_FILE_BYTES),
        (TelegramMediaKind.DOCUMENT, MAX_TELEGRAM_FILE_BYTES),
    ],
)
def test_delivery_media_enforces_telegram_size_limit(
    kind: TelegramMediaKind,
    limit: int,
) -> None:
    mime_type = {
        TelegramMediaKind.PHOTO: "image/png",
        TelegramMediaKind.VIDEO: "video/mp4",
        TelegramMediaKind.AUDIO: "audio/mpeg",
        TelegramMediaKind.VOICE: "audio/ogg",
        TelegramMediaKind.DOCUMENT: "application/octet-stream",
    }[kind]

    with pytest.raises(ArtifactTooLargeError) as error:
        DeliveryMedia(kind, mime_type, bytes(limit + 1))

    assert error.value.size_bytes == limit + 1
    assert error.value.limit_bytes == limit


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        ((TelegramMediaKind.PHOTO,), DeliveryBatchKind.SINGLE),
        (
            (TelegramMediaKind.PHOTO, TelegramMediaKind.VIDEO),
            DeliveryBatchKind.VISUAL_ALBUM,
        ),
        (
            (TelegramMediaKind.AUDIO, TelegramMediaKind.AUDIO),
            DeliveryBatchKind.AUDIO_ALBUM,
        ),
        (
            (TelegramMediaKind.DOCUMENT, TelegramMediaKind.DOCUMENT),
            DeliveryBatchKind.DOCUMENT_ALBUM,
        ),
    ],
)
def test_delivery_batch_classification_selects_one_atomic_telegram_call(
    kinds: tuple[TelegramMediaKind, ...],
    expected: DeliveryBatchKind,
) -> None:
    assert classify_delivery_batch(kinds) is expected


@pytest.mark.parametrize(
    "kinds",
    [
        (),
        (TelegramMediaKind.VOICE, TelegramMediaKind.VOICE),
        (TelegramMediaKind.AUDIO, TelegramMediaKind.DOCUMENT),
        (TelegramMediaKind.PHOTO, TelegramMediaKind.AUDIO),
        (TelegramMediaKind.PHOTO,) * (MAX_TELEGRAM_ALBUM_ITEMS + 1),
    ],
)
def test_delivery_batch_classification_rejects_non_atomic_shapes(
    kinds: tuple[TelegramMediaKind, ...],
) -> None:
    with pytest.raises(ValueError):
        classify_delivery_batch(kinds)
