"""Audio conversion utilities using ffmpeg.

Provides async audio format conversion for TTS voice messages.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import logfire


class AudioConversionError(Exception):
    """Raised when audio conversion fails."""


async def convert_to_ogg_opus(
    audio_bytes: bytes,
    *,
    input_format: str = "wav",
    sample_rate: int | None = None,
    channels: int = 1,
) -> bytes:
    """Convert audio bytes to OGG/Opus format using ffmpeg.

    Args:
        audio_bytes: Raw audio data to convert.
        input_format: Input format hint for ffmpeg (e.g., "wav", "s16le" for raw PCM).
        sample_rate: Sample rate in Hz. Required for raw PCM formats like "s16le".
        channels: Number of audio channels (default: 1 for mono).

    Returns:
        Audio data encoded as OGG/Opus.

    Raises:
        AudioConversionError: If ffmpeg fails or is not available.
    """
    if not isinstance(audio_bytes, bytes):
        raise TypeError("audio_bytes must be immutable bytes")
    if not audio_bytes:
        raise AudioConversionError("audio input must not be empty")
    if input_format not in {"s16le", "wav"}:
        raise ValueError("input_format must be s16le or wav")
    if isinstance(channels, bool) or not isinstance(channels, int) or channels <= 0:
        raise ValueError("channels must be a positive integer")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    if input_format == "s16le":
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or sample_rate <= 0
        ):
            raise AudioConversionError("sample_rate required for raw PCM input")
        cmd.extend(["-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels)])

    cmd.extend(["-i", "pipe:0"])

    cmd.extend(
        [
            "-c:a",
            "libopus",
            "-b:a",
            "48k",
            "-vbr",
            "on",
            "-application",
            "voip",
            "-f",
            "ogg",
            "pipe:1",
        ]
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, _ = await process.communicate(input=audio_bytes)
        except BaseException:
            await _terminate_process(process)
            raise

        if process.returncode != 0:
            raise AudioConversionError("ffmpeg conversion failed")

        logfire.debug(
            "audio_converted",
            input_format=input_format,
            input_size=len(audio_bytes),
            output_size=len(stdout),
        )

        return stdout

    except FileNotFoundError as e:
        raise AudioConversionError("ffmpeg not found - is it installed?") from e
    except TimeoutError as e:
        raise AudioConversionError("ffmpeg conversion timed out") from e


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Best-effort reap a child when its caller is cancelled."""
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.kill()
    with suppress(Exception):
        await process.wait()
