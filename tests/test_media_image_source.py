"""Tests for Telegram image references and source hydration."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.types import Document, PhotoSize, Sticker

from derp.media import (
    MediaFamily,
    MediaGateway,
    MediaMetadata,
    MediaReference,
    TelegramImageSourceLoader,
    image_reference_from_telegram,
)


def test_photo_size_becomes_stable_jpeg_reference() -> None:
    photo = PhotoSize(
        file_id="downloadable-photo",
        file_unique_id="stable-photo",
        file_size=123,
        width=1280,
        height=720,
    )

    reference = image_reference_from_telegram(photo)

    assert reference == MediaReference(
        file_id="downloadable-photo",
        file_unique_id="stable-photo",
        metadata=MediaMetadata(
            mime_type="image/jpeg",
            file_size=123,
            width=1280,
            height=720,
        ),
    )


def test_image_document_preserves_declared_identity_and_filename() -> None:
    document = Document(
        file_id="downloadable-document",
        file_unique_id="stable-document",
        file_size=456,
        file_name="source.png",
        mime_type="IMAGE/PNG; charset=binary",
    )

    reference = image_reference_from_telegram(document)

    assert reference.metadata == MediaMetadata(
        mime_type="image/png",
        file_size=456,
        file_name="source.png",
    )


def test_static_sticker_becomes_webp_image_reference() -> None:
    sticker = Sticker(
        file_id="downloadable-sticker",
        file_unique_id="stable-sticker",
        type="regular",
        width=512,
        height=512,
        is_animated=False,
        is_video=False,
        file_size=789,
    )

    reference = image_reference_from_telegram(sticker)

    assert reference.metadata == MediaMetadata(
        mime_type="image/webp",
        file_size=789,
        width=512,
        height=512,
    )


@pytest.mark.parametrize(
    "media",
    [
        Document(
            file_id="missing-mime",
            file_unique_id="missing-mime-stable",
        ),
        Document(
            file_id="not-image",
            file_unique_id="not-image-stable",
            mime_type="application/pdf",
        ),
        Sticker(
            file_id="animated",
            file_unique_id="animated-stable",
            type="regular",
            width=512,
            height=512,
            is_animated=True,
            is_video=False,
        ),
        Sticker(
            file_id="video",
            file_unique_id="video-stable",
            type="regular",
            width=512,
            height=512,
            is_animated=False,
            is_video=True,
        ),
    ],
)
def test_non_static_or_non_image_metadata_is_rejected(
    media: Document | Sticker,
) -> None:
    with pytest.raises(ValueError):
        image_reference_from_telegram(media)


@pytest.mark.asyncio
async def test_source_loader_uses_injected_bot_and_shared_gateway() -> None:
    reference = MediaReference(
        file_id="downloadable",
        file_unique_id="stable",
        metadata=MediaMetadata(mime_type="image/jpeg", file_size=5),
    )
    bot = cast(Bot, MagicMock(spec=Bot))
    gateway = MagicMock(spec=MediaGateway)
    gateway.download = AsyncMock(return_value=b"image")

    result = await TelegramImageSourceLoader(gateway, bot=bot).load(reference)

    assert result == b"image"
    gateway.download.assert_awaited_once_with(
        bot=bot,
        reference=reference,
        expected_family=MediaFamily.IMAGE,
    )
