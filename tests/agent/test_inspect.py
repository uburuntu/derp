import io
from collections.abc import AsyncIterator

import pytest
from aiogram import Bot
from PIL import Image

from derp.agent.media import as_content, inspect
from derp.history.models import MessageMedia
from derp.telegram.media import MediaGateway, MediaUnavailableError
from derp.tools.workers import WorkerPool

BOT = 123456


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), "orange").save(buffer, format="PNG")
    return buffer.getvalue()


class Files(MediaGateway):
    """Serves registered files without Telegram; records what was downloaded."""

    def __init__(self, bot: Bot, files: dict[str, bytes]) -> None:
        super().__init__(bot)
        self.files = files
        self.fetched: list[str] = []

    async def fetch(
        self, file_id: str, bot_id: int, key: str | None = None, size_hint: int | None = None
    ) -> bytes:
        if size_hint and size_hint > self.max_bytes:
            raise MediaUnavailableError("too large")
        self.fetched.append(file_id)
        return self.files[file_id]


@pytest.fixture
async def bot() -> AsyncIterator[Bot]:
    bot = Bot(f"{BOT}:TEST")
    yield bot
    await bot.session.close()


def row(kind: str, file_id: str, **fields: object) -> MessageMedia:
    return MessageMedia(
        chat_id=1,
        message_id=7,
        idx=0,
        type=kind,
        file_id=file_id,
        file_unique_id=f"u-{file_id}",
        bot_id=BOT,
        **fields,
    )


async def test_a_clip_too_big_for_a_frame_is_seen_through_its_preview(bot: Bot) -> None:
    gateway = Files(bot, {"thumb": png()})
    video = row("video", "film", mime_type="video/mp4", file_size=50 * 2**20, thumb_file_id="thumb")
    (item,) = await inspect(gateway, [video], frames=WorkerPool())
    assert item.still == "preview" and item.image.media_type == "image/jpeg"
    assert as_content([item])[0] == "(the preview picture of the video from #7)"
    assert gateway.fetched == ["thumb"]


async def test_what_is_neither_a_picture_nor_has_a_preview_is_skipped_unfetched(bot: Bot) -> None:
    gateway = Files(bot, {})
    tgs = row("sticker", "lottie", mime_type="application/x-tgsticker")
    pdf = row("document", "paper", mime_type="application/pdf")
    assert await inspect(gateway, [tgs, pdf]) == []
    assert await inspect(gateway, [tgs, pdf], frames=WorkerPool()) == []  # no frame of a Lottie or a PDF
    assert gateway.fetched == []


async def test_a_video_message_is_never_downloaded_as_a_picture(bot: Bot) -> None:
    gateway = Files(bot, {"thumb": png()})
    note = row("video_note", "circle", thumb_file_id="thumb")  # no MIME type, still a clip
    (item,) = await inspect(gateway, [note])
    assert item.still == "preview" and gateway.fetched == ["thumb"]


async def test_a_file_of_unknown_type_is_downloaded_and_sniffed(bot: Bot) -> None:
    gateway = Files(bot, {"scan": png()})
    (item,) = await inspect(gateway, [row("document", "scan", mime_type="application/octet-stream")])
    assert item.still == "" and gateway.fetched == ["scan"]


async def test_an_image_file_is_seen_itself_normalized(bot: Bot) -> None:
    gateway = Files(bot, {"scan": png(), "thumb": png()})
    (item,) = await inspect(gateway, [row("document", "scan", mime_type="image/png", thumb_file_id="thumb")])
    assert item.still == "" and Image.open(io.BytesIO(item.image.data)).size == (48, 32)
    assert gateway.fetched == ["scan"]
