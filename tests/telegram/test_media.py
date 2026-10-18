import io
from typing import cast

import pytest
from aiogram import Bot
from aiogram.types import File
from PIL import Image

from derp.telegram.media import (
    VISION_SIDE,
    Media,
    MediaGateway,
    MediaUnavailableError,
    normalize,
    sniff_image,
)


def test_sniff_image_recognizes_common_formats_only() -> None:
    assert sniff_image(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image(b"\x00\x00\x00\x1cftypheic\x00\x00") == "image/heic"
    assert sniff_image(b"II*\x00rest") == "image/tiff"
    assert sniff_image(b"\x1aE\xdf\xa3webm") is None  # video stickers are not images
    assert sniff_image(b"") is None


async def test_files_of_another_bot_are_never_requested() -> None:
    bot = Bot("123456:TEST")  # no session is opened: the owner check comes first
    gateway = MediaGateway(bot)
    with pytest.raises(MediaUnavailableError, match="another bot"):
        await gateway.fetch("file", bot_id=654321, key="unique")
    await bot.session.close()


def encoded(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def decoded(media: Media) -> Image.Image:
    assert media.media_type == "image/jpeg"
    image = Image.open(io.BytesIO(media.data))
    assert image.format == "JPEG"
    return image


@pytest.mark.parametrize("fmt", ["PNG", "GIF", "WEBP", "TIFF", "BMP", "HEIF", "AVIF"])
def test_normalize_decodes_every_picture_format_to_jpeg(fmt: str) -> None:
    image = decoded(normalize(encoded(Image.new("RGB", (64, 48), "red"), fmt)))
    assert image.size == (64, 48)
    red, green, blue = cast(tuple[int, int, int], image.getpixel((32, 24)))
    assert red > 200 and green < 60 and blue < 60


def test_normalize_turns_a_photo_upright_by_its_exif_orientation() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways: rotate 90° clockwise to show
    data = encoded(Image.new("RGB", (80, 40), "blue"), "JPEG", exif=exif)
    assert decoded(normalize(data)).size == (40, 80)


def test_normalize_scales_a_big_picture_down_and_refuses_one_too_big_to_decode() -> None:
    assert max(decoded(normalize(encoded(Image.new("RGB", (4000, 3000)), "JPEG"))).size) == VISION_SIDE
    with pytest.raises(MediaUnavailableError, match="too large"):
        normalize(encoded(Image.new("1", (9000, 10)), "PNG"))  # past the side cap, tiny file
    with pytest.raises(MediaUnavailableError, match="too large"):
        normalize(encoded(Image.new("1", (5000, 4000)), "PNG"))  # 20 MP


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_normalize_puts_transparency_on_white(fmt: str) -> None:
    clear = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    clear.paste((0, 0, 0, 255), (0, 0, 10, 20))
    image = decoded(normalize(encoded(clear, fmt, lossless=True) if fmt == "WEBP" else encoded(clear, fmt)))
    assert min(cast(tuple[int, ...], image.getpixel((15, 10)))) > 240  # was transparent: white
    assert max(cast(tuple[int, ...], image.getpixel((3, 10)))) < 20  # opaque black stays black


def test_normalize_scales_deep_grayscale_to_eight_bits_instead_of_clipping_to_white() -> None:
    deep = Image.new("I;16", (16, 16), 30000)  # a 16-bit grayscale PNG: 30000 of 65535
    gray = cast(tuple[int, ...], decoded(normalize(encoded(deep, "PNG"))).getpixel((8, 8)))
    assert all(abs(channel - 30000 // 256) < 6 for channel in gray)
    floating = Image.new("F", (16, 16), 0.5)  # a float TIFF: scaled by its own range, dark to white
    floating.paste(1.0, (8, 0, 16, 16))
    image = decoded(normalize(encoded(floating, "TIFF")))
    assert max(cast(tuple[int, ...], image.getpixel((2, 8)))) < 20
    assert min(cast(tuple[int, ...], image.getpixel((13, 8)))) > 235


def test_normalize_refuses_what_is_not_a_picture() -> None:
    with pytest.raises(MediaUnavailableError, match="not an image"):
        normalize(b"\x1aE\xdf\xa3webm video sticker")
    with pytest.raises(MediaUnavailableError, match="not an image"):
        normalize(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # a signature alone isn't a picture


async def test_a_file_whose_mime_type_is_not_a_picture_is_never_downloaded() -> None:
    bot = Bot("123456:TEST")  # no session is opened: the MIME check comes before any request
    gateway = MediaGateway(bot)
    with pytest.raises(MediaUnavailableError, match="not an image"):
        await gateway.image("file", bot_id=bot.id, key="unique", mime_type="application/pdf")
    await bot.session.close()


class Stored:
    """A bot whose files are named the way Telegram stores them; records what was downloaded."""

    id = 123456

    def __init__(self, path: str) -> None:
        self.path = path
        self.downloaded: list[str] = []

    async def get_file(self, file_id: str) -> File:
        return File(file_id=file_id, file_unique_id=f"u-{file_id}", file_size=4096, file_path=self.path)

    async def download_file(self, path: str, destination: io.BytesIO) -> None:
        self.downloaded.append(path)
        destination.write(encoded(Image.new("RGB", (8, 8), "red"), "WEBP"))


async def test_an_animated_sticker_is_never_downloaded_even_when_its_row_does_not_say_so() -> None:
    bot = Stored("stickers/file_7.tgs")
    with pytest.raises(MediaUnavailableError, match="animated sticker"):
        await MediaGateway(cast(Bot, bot)).image("old-sticker", bot_id=bot.id)  # an older row: no MIME
    assert bot.downloaded == []


async def test_a_generic_byte_stream_is_downloaded_and_sniffed() -> None:
    bot = Stored("documents/file_8")
    gateway = MediaGateway(cast(Bot, bot))
    media = await gateway.image("scan", bot_id=bot.id, mime_type="application/octet-stream")
    assert media.media_type == "image/jpeg" and bot.downloaded == ["documents/file_8"]
