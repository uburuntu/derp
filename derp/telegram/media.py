"""Download Telegram files on demand, with caps and a short in-process cache.

A ``file_id`` works only for the bot that received it (``file_unique_id`` is
the same for every bot but can't be downloaded), so every fetch names the
owning bot and a file of another bot — say, rows captured by a test bot — is
unavailable rather than a Telegram error. File URLs embed the bot token: they are never logged or returned, and errors carry
only the exception type.
"""

import asyncio
import io
import time
from collections import OrderedDict
from dataclasses import dataclass

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # getFile refuses larger files
CACHE_SECONDS = 600
CACHE_BYTES = 64 * 1024 * 1024
IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class MediaUnavailableError(Exception):
    """The file can't be fetched (too big, gone, wrong type, timed out). ``reason`` is safe to show."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Media:
    data: bytes
    media_type: str


def sniff_image(data: bytes) -> str | None:
    for signature, media_type in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class MediaGateway:
    def __init__(self, bot: Bot, *, max_bytes: int = BOT_API_DOWNLOAD_LIMIT, timeout: float = 30) -> None:
        self.bot = bot
        self.max_bytes = min(max_bytes, BOT_API_DOWNLOAD_LIMIT)
        self.timeout = timeout
        self._cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._cached_bytes = 0

    async def fetch(
        self, file_id: str, *, bot_id: int, key: str | None = None, size_hint: int | None = None
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
        self, file_id: str, *, bot_id: int, key: str | None = None, size_hint: int | None = None
    ) -> Media:
        data = await self.fetch(file_id, bot_id=bot_id, key=key, size_hint=size_hint)
        media_type = sniff_image(data)
        if media_type is None:
            raise MediaUnavailableError("not an image")
        return Media(data, media_type)

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
