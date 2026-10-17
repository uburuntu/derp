import asyncio
import shutil
from pathlib import Path

import pytest

from derp.config import Settings
from derp.history.transcription import Transcriber, extract_audio


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_video_note_audio_is_extracted_as_ogg(tmp_path: Path) -> None:
    target = tmp_path / "note.mp4"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(target),
    )  # fmt: skip
    assert await process.wait() == 0
    audio = await extract_audio(target.read_bytes())
    assert audio.startswith(b"OggS")
    with pytest.raises(ValueError, match="ffmpeg"):
        await extract_audio(b"not a video")


class SlowTranscriber(Transcriber):
    """Transcription takes ``seconds``; what the database would remember is kept in ``heard``."""

    def __init__(self, settings: Settings, seconds: float) -> None:
        super().__init__(db=None, api=None, gateway=None, pins=None, settings=settings)  # type: ignore[arg-type]
        self.seconds = seconds
        self.heard: list[tuple[int, int]] = []
        self.calls = 0

    async def _transcribe(self, chat_id: int, message_id: int, *, ambient: bool) -> str | None:
        self.calls += 1
        if (chat_id, message_id) in self.heard:  # already has a transcript: a no-op, like the real query
            return None
        await asyncio.sleep(self.seconds)
        self.heard.append((chat_id, message_id))
        return "hi"


async def test_a_run_waits_for_an_earlier_voice_note_still_being_transcribed(settings: Settings) -> None:
    transcriber = SlowTranscriber(settings, 0.2)
    running = asyncio.create_task(transcriber.transcribe_message(-1, 10, thread_id=None))
    await asyncio.sleep(0)  # the capture of the voice note has begun
    await transcriber.settle(-1, None, before=11)
    assert transcriber.heard == [(-1, 10)]  # its transcript is in before the run reads the chat
    await running


@pytest.mark.parametrize(
    ("chat", "thread", "before"),
    [(-2, None, 11), (-1, 7, 11), (-1, None, 10)],  # another chat, another topic, a later message
)
async def test_runs_dont_wait_for_unrelated_transcriptions(
    settings: Settings, chat: int, thread: int | None, before: int
) -> None:
    transcriber = SlowTranscriber(settings, 0.5)
    running = asyncio.create_task(transcriber.transcribe_message(-1, 10, thread_id=None))
    await asyncio.sleep(0)
    await transcriber.settle(chat, thread, before)
    assert transcriber.heard == []
    await running


async def test_waiting_is_bounded(settings: Settings) -> None:
    transcriber = SlowTranscriber(settings, 1.0)
    running = asyncio.create_task(transcriber.transcribe_message(-1, 10))
    await asyncio.sleep(0)
    started = asyncio.get_running_loop().time()
    await transcriber.settle(-1, None, before=11, limit=0.05)
    assert asyncio.get_running_loop().time() - started < 0.5 and transcriber.heard == []
    await running


async def test_the_same_message_is_transcribed_once(settings: Settings) -> None:
    transcriber = SlowTranscriber(settings, 0.1)
    await asyncio.gather(  # its own capture and an invocation replying to it, at the same time
        transcriber.transcribe_message(-1, 10, ambient=True), transcriber.transcribe_message(-1, 10)
    )
    assert transcriber.heard == [(-1, 10)] and transcriber.calls == 2  # the second found it done
    assert transcriber._running == {}  # nothing left behind


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_audio_files_are_converted_whatever_their_format(tmp_path: Path) -> None:
    target = tmp_path / "memo.mp3"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(target),
    )  # fmt: skip
    assert await process.wait() == 0
    assert (await extract_audio(target.read_bytes())).startswith(b"OggS")
