"""Silent transcription of every stored voice message and video note.

Runs inline during capture, before handlers, so an invocation by voice is
answered from its transcript. Creator-funded and ledgered as ``kind='stt'``;
nothing is posted. Video notes go through ffmpeg (audio only) first. Long
recordings, failed downloads and provider errors leave the message with an
empty transcript (like an image whose note came back empty) and the reason in
``transcript_model``, so the transcript line can say it — they never fail
capture; a failed one is tried again when an invocation replies to it. Which
messages get transcribed is decided here (``transcribe``). Ambient voice (not
part of an invocation) stops being transcribed once the day's creator-paid STT reaches
``ambient_stt_daily_usd``; owners get one ping per UTC day when that happens.
Audio files (forwarded memos, recordings) are transcribed only as part of an
invocation, never ambient; anything but OGG goes through ffmpeg first.

Updates are handled concurrently, so a run may start while a voice note sent
just before it is still being transcribed: ``settle`` lets it wait for those,
briefly, before it reads the chat.
"""

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

from aiogram.types import Message as TgMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.capture import content_type
from derp.history.models import TOO_LONG, UNHEARD, Message, MessageMedia
from derp.inference.api import OpenRouterAPI, OpenRouterError, Transcript
from derp.inference.ledger import record_usage, simple_usage
from derp.inference.models import ProviderCall
from derp.inference.pins import PinStore
from derp.inference.roles import ModelRole
from derp.telegram.media import MediaGateway

log = logging.getLogger("derp.transcription")
SPOKEN = ("voice", "video_note")  # transcribed whenever stored
AUDIBLE = (*SPOKEN, "audio")  # audio files: only as part of an invocation
FFMPEG_SECONDS = 60
SETTLE_SECONDS = 5.0  # how long a run waits for transcriptions still running before its message
Notify = Callable[[str], Awaitable[None]]


async def stt_spent_today(session: AsyncSession, now: datetime) -> Decimal:
    """Creator-paid speech-to-text since the start of the UTC day."""
    spent = await session.scalar(
        select(func.coalesce(func.sum(ProviderCall.cost_usd), 0)).where(
            ProviderCall.kind == "stt",
            ProviderCall.funding == "creator",
            ProviderCall.created_at >= datetime.combine(now.date(), time(), tzinfo=UTC),
        )
    )
    return Decimal(spent or 0)


async def extract_audio(media: bytes) -> bytes:
    """Audio track of a video note or audio file as OGG/Opus (MP4 needs a seekable input, hence the temp file).

    ffmpeg recognizes the container from its content, so the file needs no (user-supplied) extension.
    """
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "input"
        source.write_bytes(media)
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
        self,
        *,
        db: Database,
        api: OpenRouterAPI,
        gateway: MediaGateway,
        pins: PinStore,
        settings: Settings,
        notify: Notify | None = None,
    ) -> None:
        self.db = db
        self.api = api
        self.gateway = gateway
        self.pins = pins
        self.max_seconds = settings.media.voice_max_seconds
        self.ambient_cap = Decimal(str(settings.ambient_stt_daily_usd))
        self.notify = notify
        self._cap_reported: date | None = None
        # (chat, message) → (forum topic, done): transcriptions running now, for ``settle``.
        self._running: dict[tuple[int, int], tuple[int | None, asyncio.Future[None]]] = {}

    async def _over_ambient_cap(self) -> bool:
        now = datetime.now(UTC)
        async with self.db.session() as session:
            if await stt_spent_today(session, now) < self.ambient_cap:
                return False
        if self.notify is not None and self._cap_reported != now.date():
            self._cap_reported = now.date()
            await self.notify(f"Ambient voice transcription hit today's ${self.ambient_cap:g} cap.")
        return True

    async def settle(
        self, chat_id: int, thread_id: int | None, before: int, *, limit: float = SETTLE_SECONDS
    ) -> None:
        """Wait, at most ``limit`` seconds, for transcriptions of this chat topic's messages before ``before``."""
        waiting = [
            done
            for (chat, message), (thread, done) in self._running.items()
            if chat == chat_id and thread == thread_id and message < before
        ]
        if not waiting:
            return
        _, pending = await asyncio.wait(waiting, timeout=limit)
        log.info("waited for %d transcription(s), %d still running", len(waiting), len(pending))

    async def transcribe(
        self, message: TgMessage, *, thread_id: int | None = None, ambient: bool = False
    ) -> str | None:
        """The transcript of a stored voice message, video note or (not ambient) audio file; others are skipped."""
        if content_type(message) not in (SPOKEN if ambient else AUDIBLE):
            return None
        return await self.transcribe_message(
            message.chat.id, message.message_id, thread_id=thread_id, ambient=ambient
        )

    async def transcribe_message(
        self, chat_id: int, message_id: int, *, thread_id: int | None = None, ambient: bool = False
    ) -> str | None:
        """The message's transcript, transcribing it first if it has none (None: there is none to have).

        ``ambient`` voice only while under the daily cap. Runs in this chat topic may ``settle`` on it meanwhile;
        a second request while it runs waits for it and reports its transcript.
        """
        key = (chat_id, message_id)
        if (running := self._running.get(key)) is not None:  # already under way: wait for it, never pay twice
            await asyncio.shield(running[1])
            return await self.transcribe_message(chat_id, message_id, thread_id=thread_id, ambient=ambient)
        entry = (thread_id, asyncio.get_running_loop().create_future())
        self._running[key] = entry
        try:
            return await self._transcribe(chat_id, message_id, ambient=ambient)
        finally:
            if self._running.get(key) is entry:
                del self._running[key]
            entry[1].set_result(None)

    async def _transcribe(self, chat_id: int, message_id: int, *, ambient: bool) -> str | None:
        async with self.db.session() as session:
            media = await session.scalar(
                select(MessageMedia)
                .where(
                    MessageMedia.chat_id == chat_id,
                    MessageMedia.message_id == message_id,
                    MessageMedia.type.in_(SPOKEN if ambient else AUDIBLE),
                )
                .order_by(MessageMedia.idx)  # the message's own recording before one it quotes
                .limit(1)
            )
            user_id = await session.scalar(
                select(Message.user_id).where(Message.chat_id == chat_id, Message.message_id == message_id)
            )
        if media is None:
            return None
        if (
            media.transcript is not None and media.transcript_model != UNHEARD
        ):  # settled: heard, or never will be
            return media.transcript or None
        if (media.duration or 0) > self.max_seconds:
            await self._mark(media, TOO_LONG)
            return None
        if ambient and await self._over_ambient_cap():
            return None
        try:
            data = await self.gateway.fetch(
                media.file_id, bot_id=media.bot_id, key=media.file_unique_id, size_hint=media.file_size
            )
            ogg = media.type == "voice" or (media.type == "audio" and media.mime_type == "audio/ogg")
            audio = data if ogg else await extract_audio(data)
            transcript = await self._speech_to_text(audio, media.duration or 0)
        except Exception as error:  # noqa: BLE001 - capture never fails over a transcript (ffmpeg, network…)
            log.info("no transcript for a %s: %s", media.type, type(error).__name__)
            await self._mark(media, UNHEARD)
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

    async def _mark(self, media: MessageMedia, reason: str) -> None:
        """An empty transcript and why, so the transcript line can tell the model."""
        if media.transcript == "" and media.transcript_model == reason:
            return
        async with self.db.transaction() as session:
            row = await session.get(MessageMedia, (media.chat_id, media.message_id, media.idx))
            if row is not None and not row.transcript:
                row.transcript, row.transcript_model = "", reason

    async def _speech_to_text(self, audio: bytes, seconds: int) -> Transcript:
        pin = await self.pins.get(ModelRole.STT)
        error: OpenRouterError = OpenRouterError(503, "no speech-to-text model pinned")
        for model in pin.models:  # the next model is a different provider: worth a try on any error
            try:
                return await self.api.transcribe(model, audio, "ogg", zdr=pin.zdr, seconds=seconds)
            except OpenRouterError as failed:
                error = failed
        raise error
