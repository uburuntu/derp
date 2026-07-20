"""Telegram image references and bounded feature-source hydration."""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import Document, PhotoSize, Sticker

from derp.media.gateway import MediaGateway
from derp.media.types import (
    MediaFamily,
    MediaMetadata,
    MediaReference,
    normalize_mime_type,
)

type TelegramImageMetadata = PhotoSize | Document | Sticker


def image_reference_from_telegram(media: TelegramImageMetadata) -> MediaReference:
    """Capture a stable reference from one Telegram image attachment."""
    if isinstance(media, PhotoSize):
        mime_type = "image/jpeg"
        file_name = None
        width = media.width
        height = media.height
    elif isinstance(media, Document):
        if media.mime_type is None:
            raise ValueError("image document must declare a MIME type")
        mime_type = normalize_mime_type(media.mime_type)
        if not mime_type.startswith("image/"):
            raise ValueError("document must contain image media")
        file_name = media.file_name
        width = None
        height = None
    elif isinstance(media, Sticker):
        if media.is_animated or media.is_video:
            raise ValueError("image source sticker must be static")
        mime_type = "image/webp"
        file_name = None
        width = media.width
        height = media.height
    else:
        raise TypeError("media must be Telegram image metadata")

    return MediaReference(
        file_id=media.file_id,
        file_unique_id=media.file_unique_id,
        metadata=MediaMetadata(
            mime_type=mime_type,
            file_size=media.file_size,
            file_name=file_name,
            width=width,
            height=height,
        ),
    )


class TelegramImageSourceLoader:
    """Hydrate image references through the shared bounded media gateway."""

    def __init__(self, gateway: MediaGateway, *, bot: Bot) -> None:
        self._gateway = gateway
        self._bot = bot

    async def load(self, reference: MediaReference) -> bytes:
        """Load one image using the injected Bot and shared transport."""
        return await self._gateway.download(
            bot=self._bot,
            reference=reference,
            expected_family=MediaFamily.IMAGE,
        )


__all__ = [
    "TelegramImageMetadata",
    "TelegramImageSourceLoader",
    "image_reference_from_telegram",
]
