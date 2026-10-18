"""Download Telegram files on demand, with caps and a short in-process cache.

A ``file_id`` works only for the bot that received it (``file_unique_id`` is
the same for every bot but can't be downloaded), so every fetch names the
owning bot and a file of another bot — say, rows captured by a test bot — is
unavailable rather than a Telegram error. File URLs embed the bot token: they are never logged or returned, and errors carry
only the exception type.

Pictures a model sees go through one step, ``normalize``: decoded once (JPEG,
PNG, GIF, WebP, TIFF, BMP, HEIC/HEIF, AVIF; within the media workers' pixel
and side caps, ``check_dimensions``), turned upright by EXIF, scaled down to ``VISION_SIDE``,
transparency put on white, encoded as JPEG. ``MediaGateway.image`` fetches and
normalizes; a file whose MIME type says it isn't a picture is never downloaded
(``application/octet-stream`` or none says nothing: the bytes are sniffed).
An animated sticker (TGS, gzipped Lottie JSON) is useless to Pillow and
ffmpeg alike, so it is never downloaded, whatever its row says: ``fetch``
refuses a file Telegram stores as ``.tgs``.
"""

import asyncio
import io
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import cast

import aiohttp
import pillow_heif
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from PIL import Image, ImageOps

from derp.tools.workers import DEFAULTS, EffectError, check_dimensions

BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # getFile refuses larger files
CACHE_SECONDS = 600
CACHE_BYTES = 64 * 1024 * 1024
IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"BM", "image/bmp"),
)
HEIF_BRANDS = {b"heic": "image/heic", b"heix": "image/heic", b"mif1": "image/heif", b"avif": "image/avif"}
DECODABLE = ("JPEG", "PNG", "GIF", "WEBP", "TIFF", "BMP", "HEIF", "AVIF")  # never EPS, PSD & co
VISION_SIDE = 1568  # the longer side of a picture sent to a model: past this, providers scale down anyway
VISION_QUALITY = 88
UNKNOWN_TYPES = {"application/octet-stream"}  # what clients send when they don't know: sniff instead
TGS = "application/x-tgsticker"

pillow_heif.register_heif_opener()  # a format registry entry, so Pillow (and the effects) open iPhone photos


class MediaUnavailableError(Exception):
    """The file can't be fetched (too big, gone, wrong type, timed out). ``reason`` is safe to show."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Media:
    data: bytes
    media_type: str


def known_type(mime_type: str | None) -> str | None:
    """A MIME type that says something about the file, else None (missing, or a generic byte stream)."""
    return mime_type if mime_type and mime_type not in UNKNOWN_TYPES else None


def sniff_image(data: bytes) -> str | None:
    for signature, media_type in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return HEIF_BRANDS.get(data[8:12])
    return None


def _eight_bit(image: Image.Image) -> Image.Image:
    """Deep grayscale (16-bit PNG, 32-bit or float TIFF) as 8-bit ``L``: converted directly it clips to white.

    16-bit samples scale by their fixed range; 32-bit and float ones, which have none, by the picture's own.
    """
    if image.mode.startswith("I;16"):
        return image.convert("I").point(lambda value: value / 256).convert("L")
    if image.mode in ("I", "F"):
        low, high = cast(tuple[float, float], image.getextrema())
        scale = 255 / (high - low) if high > low else 1
        return image.point(lambda value: (value - low) * scale).convert("L")
    return image


def normalize(data: bytes, max_pixels: int = DEFAULTS.max_pixels) -> Media:
    """A picture as a model should get it: upright, at most ``VISION_SIDE``, opaque, JPEG (an animation's first frame).

    Raises ``MediaUnavailableError`` for anything that isn't a picture or would decode past the caps. Pillow
    is CPU-bound: callers run it off the event loop (``MediaGateway.normalize``).
    """
    try:
        with Image.open(io.BytesIO(data), formats=DECODABLE) as source:
            check_dimensions(*source.size, max_pixels)
            source.draft("RGB", (VISION_SIDE, VISION_SIDE))  # a big JPEG decodes at a fraction of its size
            upright = _eight_bit(ImageOps.exif_transpose(source))
            clear = upright.mode in ("RGBA", "LA", "PA") or "transparency" in upright.info
            image = upright.convert("RGBA" if clear else "RGB")
            image.thumbnail((VISION_SIDE, VISION_SIDE), Image.Resampling.LANCZOS)
            if clear:
                flat = Image.new("RGB", image.size, "white")
                flat.paste(image, mask=image.getchannel("A"))
                image = flat
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=VISION_QUALITY, optimize=True)
    except EffectError as refused:
        raise MediaUnavailableError(refused.reason) from None
    except OSError, ValueError, EOFError, SyntaxError, Image.DecompressionBombError:
        raise MediaUnavailableError("not an image") from None
    return Media(buffer.getvalue(), "image/jpeg")


class MediaGateway:
    def __init__(
        self,
        bot: Bot,
        *,
        max_bytes: int = BOT_API_DOWNLOAD_LIMIT,
        timeout: float = 30,
        max_pixels: int = DEFAULTS.max_pixels,
    ) -> None:
        self.bot = bot
        self.max_bytes = min(max_bytes, BOT_API_DOWNLOAD_LIMIT)
        self.timeout = timeout
        self.max_pixels = max_pixels
        self._cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._cached_bytes = 0

    async def fetch(
        self, file_id: str, bot_id: int, key: str | None = None, size_hint: int | None = None
    ) -> bytes:
        """Bytes of a file owned by ``bot_id``; ``key`` (the file_unique_id) makes repeated fetches free for a while."""
        if bot_id != self.bot.id:
            raise MediaUnavailableError("received by another bot")
        cache_key = key or file_id
        if (hit := self._cache.get(cache_key)) and time.monotonic() - hit[0] < CACHE_SECONDS:
            self._cache.move_to_end(cache_key)
            return hit[1]
        if size_hint and size_hint > self.max_bytes:
            raise MediaUnavailableError("too large")
        try:
            async with asyncio.timeout(self.timeout):
                file = await self.bot.get_file(file_id)
                if file.file_size and file.file_size > self.max_bytes:
                    raise MediaUnavailableError("too large")
                if not file.file_path:
                    raise MediaUnavailableError("not downloadable")
                if file.file_path.endswith(".tgs"):  # Telegram names files by type; rows may not know it
                    raise MediaUnavailableError("an animated sticker")
                buffer = io.BytesIO()
                await self.bot.download_file(file.file_path, buffer)
        except TimeoutError:
            raise MediaUnavailableError("timed out") from None
        except TelegramAPIError as error:
            raise MediaUnavailableError(f"telegram refused ({type(error).__name__})") from None
        except (aiohttp.ClientError, OSError) as error:  # file download failures surface from aiohttp
            raise MediaUnavailableError(f"download failed ({type(error).__name__})") from None
        data = buffer.getvalue()
        if len(data) > self.max_bytes:
            raise MediaUnavailableError("too large")
        self._remember(cache_key, data)
        return data

    async def image(
        self,
        file_id: str,
        bot_id: int,
        key: str | None = None,
        size_hint: int | None = None,
        *,
        mime_type: str | None = None,
    ) -> Media:
        """A file as a picture for a model (``normalize``); one whose ``mime_type`` isn't an image is never fetched."""
        if (known := known_type(mime_type)) and not known.startswith("image/"):
            raise MediaUnavailableError("not an image")
        return await self.normalize(await self.fetch(file_id, bot_id=bot_id, key=key, size_hint=size_hint))

    async def normalize(self, data: bytes) -> Media:
        """``normalize`` off the event loop: for pictures that didn't come from a file (avatars, kept results)."""
        return await asyncio.to_thread(normalize, data, self.max_pixels)

    def _remember(self, key: str, data: bytes) -> None:
        if len(data) > CACHE_BYTES // 4:
            return
        if (old := self._cache.pop(key, None)) is not None:
            self._cached_bytes -= len(old[1])
        self._cache[key] = (time.monotonic(), data)
        self._cached_bytes += len(data)
        while self._cached_bytes > CACHE_BYTES and self._cache:
            _, (_, evicted) = self._cache.popitem(last=False)
            self._cached_bytes -= len(evicted)


async def profile_photo_bytes(bot: Bot, gateway: MediaGateway, user_id: int) -> bytes:
    """A person's current profile photo, largest size; raises when Telegram doesn't share one."""
    try:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
    except TelegramAPIError:
        raise MediaUnavailableError("Telegram didn't share that profile photo") from None
    if not photos.photos:
        raise MediaUnavailableError("no profile photo visible to Derp")
    biggest = photos.photos[0][-1]
    return await gateway.fetch(
        biggest.file_id, bot_id=bot.id, key=biggest.file_unique_id, size_hint=biggest.file_size
    )


async def chat_photo_bytes(bot: Bot, gateway: MediaGateway, chat_id: int) -> bytes:
    """A chat's own photo (a private chat's is the person's)."""
    try:
        photo = (await bot.get_chat(chat_id)).photo
    except TelegramAPIError:
        raise MediaUnavailableError("Telegram didn't share the chat photo") from None
    if photo is None:
        raise MediaUnavailableError("this chat has no photo")
    return await gateway.fetch(photo.big_file_id, bot_id=bot.id, key=photo.big_file_unique_id)
