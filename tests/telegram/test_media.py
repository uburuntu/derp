import pytest
from aiogram import Bot

from derp.telegram.media import MediaGateway, MediaUnavailableError, sniff_image


def test_sniff_image_recognizes_common_formats_only() -> None:
    assert sniff_image(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image(b"\x1aE\xdf\xa3webm") is None  # video stickers are not images
    assert sniff_image(b"") is None


async def test_files_of_another_bot_are_never_requested() -> None:
    bot = Bot("123456:TEST")  # no session is opened: the owner check comes first
    gateway = MediaGateway(bot)
    with pytest.raises(MediaUnavailableError, match="another bot"):
        await gateway.fetch("file", bot_id=654321, key="unique")
    await bot.session.close()
