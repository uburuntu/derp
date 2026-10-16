"""Media effects on bytes: captioned memes (images and video), reversing, background removal, samples.

Each public function takes a worker slot, works in a throwaway directory and
returns the result's bytes. ffmpeg only ever reads local files of whitelisted
container formats (no playlists or network protocols) and never sees user
text: captions are drawn by Pillow and overlaid as a picture.
"""

import asyncio
import contextlib
import json
import math
import sys
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from derp.tools import captions
from derp.tools.workers import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    POOL,
    EffectError,
    check_dimensions,
    run,
)

# Containers chat media arrives in; playlists, concat lists and session descriptions are excluded.
FORMATS = "mov,mp4,matroska,webm,avi,mpegts,ogg,gif,image2,png_pipe,jpeg_pipe,webp_pipe,wav,mp3,aac,flac"
INPUT = ("-protocol_whitelist", "file", "-format_whitelist", FORMATS)
MAX_VIDEO_SECONDS = 60
MAX_REVERSE_SECONDS = 30
MAX_REVERSE_BUFFER = 512 * 1024 * 1024  # ffmpeg holds the whole decoded clip to reverse it
SAMPLE_SECONDS = 15
STREAMS = "stream=codec_type,width,height,avg_frame_rate,duration:stream_side_data=rotation:format=duration"
BACKGROUND_TIMEOUT = 45


@dataclass(frozen=True, slots=True)
class Probe:
    width: int  # displayed (rotation applied); 0 without video
    height: int
    seconds: float
    fps: float
    audio: bool

    @property
    def video(self) -> bool:
        return self.width > 0


def _number(value: object) -> float:
    try:
        number = float(Fraction(str(value)))
    except ValueError, ZeroDivisionError, OverflowError:
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _rotation(stream: dict[str, Any]) -> int:
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict) and "rotation" in side:
            return round(_number(abs(float(side["rotation"])))) % 360
    return 0


async def probe(source: Path) -> Probe:
    raw = await run(
        [
            "ffprobe", "-v", "error", *INPUT,
            "-show_entries", STREAMS,
            "-of", "json", str(source),
        ],
        seconds=10, max_output=1 << 20, what="probe",
    )  # fmt: skip
    try:
        data = json.loads(raw)
        streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
        total = _number((data.get("format") or {}).get("duration"))
    except (ValueError, AttributeError) as error:
        raise EffectError("unreadable media") from error
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    width = height = 0
    fps = 0.0
    if video is not None:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        check_dimensions(width, height)
        if _rotation(video) in (90, 270):
            width, height = height, width
        fps = _number(video.get("avg_frame_rate"))
        total = max(total, _number(video.get("duration")))
    audio = any(s.get("codec_type") == "audio" for s in streams)
    return Probe(width, height, total, fps, audio)


def _ffmpeg(*args: str) -> list[str]:
    return ["ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2", "-filter_threads", "1", *args]


@contextlib.asynccontextmanager
async def _workspace(data: bytes) -> AsyncIterator[Path]:
    """A worker slot and a throwaway directory holding the input as ``source``."""
    if len(data) > MAX_INPUT_BYTES:
        raise EffectError("the file is too large (20 MB at most)")
    async with POOL.slot():
        with tempfile.TemporaryDirectory(prefix="derp-fx-") as directory:
            source = Path(directory, "source")
            await asyncio.to_thread(source.write_bytes, data)
            yield source


async def _render(source: Path, name: str, args: list[str], *, what: str, read: float | None = None) -> bytes:
    """``read`` caps how many seconds of input are decoded, whatever the container claims."""
    output = source.with_name(name)
    limit = ["-t", str(read)] if read else []
    await run(
        _ffmpeg(*INPUT, *limit, "-i", str(source), *args, "-fs", str(MAX_OUTPUT_BYTES), str(output)),
        what=what,
    )
    data = await asyncio.to_thread(output.read_bytes)
    if not data or len(data) >= MAX_OUTPUT_BYTES:
        raise EffectError(f"{what} output too large")
    return data


VIDEO_OUT = [
    "-map_metadata", "-1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
]  # fmt: skip


async def meme_image(data: bytes, text: str, style: captions.Style) -> bytes:
    """The captioned picture as JPEG."""
    if len(data) > MAX_INPUT_BYTES:
        raise EffectError("the file is too large (20 MB at most)")
    async with POOL.slot():
        return await asyncio.to_thread(captions.caption_image, data, text, style)


async def meme_video(data: bytes, text: str, style: captions.Style, *, sound: bool = True) -> bytes:
    """The captioned clip as MP4 (H.264/AAC), timeline and sound kept."""
    async with _workspace(data) as source:
        info = await probe(source)
        if not info.video:
            raise EffectError("no video in that file")
        if info.seconds > MAX_VIDEO_SECONDS:
            raise EffectError(f"the clip is too long ({MAX_VIDEO_SECONDS} s at most)")
        width, height = captions.media_size(info.width, info.height)
        frame = await asyncio.to_thread(captions.caption_frame, (width, height), text, style)
        overlay = source.with_name("caption.png")
        await asyncio.to_thread(frame.overlay.save, overlay, "PNG")
        out_w, out_h = (
            frame.overlay.width + frame.overlay.width % 2,
            frame.overlay.height + frame.overlay.height % 2,
        )
        x, y = frame.position
        graph = (
            f"[0:v:0]scale={width}:{height}:flags=lanczos,setsar=1,format=rgba,"
            f"pad={out_w}:{out_h}:{x}:{y}:color=black[v];"
            "[v][1:v:0]overlay=0:0:eof_action=repeat:repeatlast=1:format=auto,format=yuv420p[out]"
        )
        args = [
            "-protocol_whitelist", "file", "-i", str(overlay), "-filter_complex", graph,
            "-map", "[out]", *(["-map", "0:a:0?"] if sound and info.audio else ["-an"]),
            "-t", str(MAX_VIDEO_SECONDS), *VIDEO_OUT,
        ]  # fmt: skip
        return await _render(source, "meme.mp4", args, what="video meme")


async def reverse(data: bytes, *, voice: bool = False) -> bytes:
    """Play it backwards: MP4 for clips, Ogg/Opus for voice and audio."""
    async with _workspace(data) as source:
        info = await probe(source)
        if info.seconds <= 0:
            raise EffectError("can't tell how long that is")
        if info.seconds > MAX_REVERSE_SECONDS:
            raise EffectError(f"too long to reverse ({MAX_REVERSE_SECONDS} s at most)")
        if voice or not info.video:
            if not info.audio:
                raise EffectError("no sound in that file")
            args = ["-vn", "-af", "areverse", "-c:a", "libopus", "-b:a", "64k", "-f", "ogg"]
            return await _render(source, "reversed.ogg", args, what="reverse", read=MAX_REVERSE_SECONDS)
        width, height = captions.media_size(info.width, info.height)
        if width * height * 4 * info.seconds * (info.fps or 30) > MAX_REVERSE_BUFFER:
            raise EffectError("too big to reverse; try a shorter or smaller clip")
        args = [
            "-vf", f"scale={width}:{height}:flags=lanczos,reverse",
            *(["-af", "areverse"] if info.audio else ["-an"]), *VIDEO_OUT,
        ]  # fmt: skip
        return await _render(source, "reversed.mp4", args, what="reverse", read=MAX_REVERSE_SECONDS)


async def mirror(data: bytes) -> bytes:
    """A picture flipped left to right, as JPEG."""
    if len(data) > MAX_INPUT_BYTES:
        raise EffectError("the file is too large (20 MB at most)")
    async with POOL.slot():
        return await asyncio.to_thread(captions.mirror, data)


async def still(data: bytes) -> bytes:
    """One JPEG frame from a clip (a second in, or the first frame of a short one)."""
    async with _workspace(data) as source:
        info = await probe(source)
        if not info.video:
            raise EffectError("no picture in that file")
        seek = ["-ss", "1"] if info.seconds > 2 else []
        args = [*seek, "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-c:v", "mjpeg", "-f", "image2"]
        return await _render(source, "still.jpg", args, what="frame")


async def audio_sample(data: bytes) -> bytes:
    """The first seconds of the sound as small mono WAV, for song recognition."""
    async with _workspace(data) as source:
        if not (await probe(source)).audio:
            raise EffectError("no sound in that file")
        args = ["-vn", "-t", str(SAMPLE_SECONDS), "-ac", "1", "-ar", "16000", "-f", "wav"]
        return await _render(source, "sample.wav", args, what="sample")


async def remove_background(data: bytes, model: Path) -> bytes:
    """A transparent-background PNG from the local U²-NetP model, in its own killable process."""
    async with _workspace(data) as source:
        return await run(
            [sys.executable, "-m", "derp.tools.background", str(source), str(model)],
            seconds=BACKGROUND_TIMEOUT,
            what="background removal",
        )
