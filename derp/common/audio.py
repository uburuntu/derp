"""Audio conversion utilities using ffmpeg.

Provides async audio format conversion for TTS voice messages.
"""

from __future__ import annotations

import asyncio

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
    # Build ffmpeg command
    # -i pipe:0  = read from stdin
    # -f ogg     = output format
    # -c:a libopus = Opus codec
    # pipe:1     = write to stdout
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    # Input format options
    if input_format == "s16le":
        # Raw PCM needs explicit format specification
        if sample_rate is None:
            raise AudioConversionError("sample_rate required for raw PCM input")
        cmd.extend(["-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels)])

    cmd.extend(["-i", "pipe:0"])

    # Output format options
    cmd.extend(
        [
            "-c:a",
            "libopus",
            "-b:a",
            "48k",  # Good quality for voice
            "-vbr",
            "on",
            "-application",
            "voip",  # Optimized for speech
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

        stdout, stderr = await process.communicate(input=audio_bytes)

        if process.returncode != 0:
            error_msg = stderr.decode(errors="replace").strip()
            logfire.warning(
                "ffmpeg_conversion_failed",
                returncode=process.returncode,
                stderr=error_msg[:500],
                input_size=len(audio_bytes),
            )
            raise AudioConversionError(f"ffmpeg failed: {error_msg}")

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
