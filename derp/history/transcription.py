"""Silent transcription of every stored voice message and video note.

Runs inline during capture, before handlers, so an invocation by voice is
answered from its transcript. Creator-funded and ledgered as ``kind='stt'``;
nothing is posted. Video notes go through ffmpeg (audio only) first. Long
recordings, failed downloads and provider errors leave the message without a
transcript — they never fail capture.
"""

import asyncio
import logging
import tempfile
from pathlib import Path

from sqlalchemy import select

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Message, MessageMedia
from derp.inference.api import OpenRouterAPI, OpenRouterError, Transcript
from derp.inference.ledger import record_usage, simple_usage
from derp.inference.pins import PinStore
from derp.inference.roles import ModelRole
from derp.telegram.media import MediaGateway, MediaUnavailableError

log = logging.getLogger("derp.transcription")
SPOKEN = ("voice", "video_note")
FFMPEG_SECONDS = 60


async def extract_audio(video: bytes) -> bytes:
    """Audio track of a video note as OGG/Opus (MP4 needs a seekable input, hence the temp file)."""
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "note.mp4"
        source.write_bytes(video)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", str(source), "-vn", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        try:
            async with asyncio.timeout(FFMPEG_SECONDS):
                audio, _ = await process.communicate()
        except TimeoutError:
            process.kill()
            raise
    if process.returncode != 0 or not audio:
        raise ValueError("ffmpeg could not extract audio")
    return audio


class Transcriber:
    def __init__(
        self, *, db: Database, api: OpenRouterAPI, gateway: MediaGateway, pins: PinStore, settings: Settings
    ) -> None:
        self.db = db
        self.api = api
        self.gateway = gateway
        self.pins = pins
        self.max_seconds = settings.media.voice_max_seconds

    async def transcribe_message(self, chat_id: int, message_id: int) -> str | None:
        async with self.db.session() as session:
            media = await session.scalar(
                select(MessageMedia).where(
                    MessageMedia.chat_id == chat_id,
                    MessageMedia.message_id == message_id,
                    MessageMedia.type.in_(SPOKEN),
                    MessageMedia.transcript.is_(None),
                )
            )
            user_id = await session.scalar(
                select(Message.user_id).where(Message.chat_id == chat_id, Message.message_id == message_id)
            )
        if media is None or (media.duration or 0) > self.max_seconds:
            return None
        try:
            data = await self.gateway.fetch(
                media.file_id, key=media.file_unique_id, size_hint=media.file_size
            )
            audio = await extract_audio(data) if media.type == "video_note" else data
            transcript = await self._transcribe(audio)
        except (MediaUnavailableError, OpenRouterError, ValueError, TimeoutError) as error:
            log.info("no transcript for a %s: %s", media.type, type(error).__name__)
            return None
        async with self.db.transaction() as session:
            record_usage(
                session,
                simple_usage(transcript.model, transcript.cost_usd),
                kind="stt",
                funding="creator",
                role=ModelRole.STT.value,
                chat_id=chat_id,
                user_id=user_id,
                units=media.duration or 0,
            )
            row = await session.get(MessageMedia, (media.chat_id, media.message_id, media.idx))
            message = await session.get(Message, (chat_id, message_id))
            if row is None or message is None:
                return None
            row.transcript = transcript.text
            row.transcript_model = transcript.model
            message.extra_text = " ".join(filter(None, (message.extra_text, transcript.text))) or None
        return transcript.text

    async def _transcribe(self, audio: bytes) -> Transcript:
        pin = await self.pins.get(ModelRole.STT)
        error: OpenRouterError = OpenRouterError(503, "no speech-to-text model pinned")
        for model in pin.models:  # the next model is a different provider: worth a try on any error
            try:
                return await self.api.transcribe(model, audio, "ogg", zdr=pin.zdr)
            except OpenRouterError as failed:
                error = failed
        raise error
