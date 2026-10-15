import asyncio
import shutil
from pathlib import Path

import pytest

from derp.history.transcription import extract_audio


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
