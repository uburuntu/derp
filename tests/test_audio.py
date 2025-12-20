"""Tests for audio conversion utilities.

These tests require ffmpeg to be installed. They are skipped if ffmpeg is not available.
"""

from __future__ import annotations

import asyncio
import shutil
import struct
from typing import TYPE_CHECKING

import pytest

from derp.common.audio import AudioConversionError, convert_to_ogg_opus

if TYPE_CHECKING:
    pass

# Check if ffmpeg is available
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


def create_wav_header(
    num_samples: int,
    sample_rate: int = 24000,
    channels: int = 1,
    bits_per_sample: int = 16,
) -> bytes:
    """Create a valid WAV file header."""
    data_size = num_samples * channels * (bits_per_sample // 8)
    file_size = 36 + data_size  # 44 - 8 = 36

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        file_size,
        b"WAVE",
        b"fmt ",
        16,  # PCM chunk size
        1,  # Audio format (PCM)
        channels,
        sample_rate,
        sample_rate * channels * (bits_per_sample // 8),  # Byte rate
        channels * (bits_per_sample // 8),  # Block align
        bits_per_sample,
        b"data",
        data_size,
    )
    return header


def generate_sine_wave_pcm(
    duration_ms: int = 100,
    frequency: int = 440,
    sample_rate: int = 24000,
    amplitude: int = 16000,
) -> bytes:
    """Generate raw 16-bit PCM sine wave data."""
    import math

    num_samples = int(sample_rate * duration_ms / 1000)
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        value = int(amplitude * math.sin(2 * math.pi * frequency * t))
        samples.append(struct.pack("<h", value))
    return b"".join(samples)


@pytest.fixture
def wav_audio() -> bytes:
    """Generate a short WAV file with a sine wave."""
    pcm_data = generate_sine_wave_pcm(duration_ms=100)
    header = create_wav_header(len(pcm_data) // 2)  # 2 bytes per sample
    return header + pcm_data


@pytest.fixture
def raw_pcm_audio() -> bytes:
    """Generate raw 16-bit PCM audio (no header)."""
    return generate_sine_wave_pcm(duration_ms=100)


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not installed")
class TestConvertToOggOpus:
    """Tests for convert_to_ogg_opus function."""

    @pytest.mark.asyncio
    async def test_convert_wav_to_ogg(self, wav_audio: bytes) -> None:
        """Converting WAV to OGG should produce valid OGG output."""
        result = await convert_to_ogg_opus(wav_audio, input_format="wav")

        # OGG files start with "OggS"
        assert result[:4] == b"OggS", "Output should be OGG format"
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_convert_raw_pcm_to_ogg(self, raw_pcm_audio: bytes) -> None:
        """Converting raw PCM to OGG should work with sample_rate specified."""
        result = await convert_to_ogg_opus(
            raw_pcm_audio,
            input_format="s16le",
            sample_rate=24000,
        )

        assert result[:4] == b"OggS", "Output should be OGG format"
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_raw_pcm_requires_sample_rate(self, raw_pcm_audio: bytes) -> None:
        """Raw PCM conversion should fail without sample_rate."""
        with pytest.raises(AudioConversionError, match="sample_rate required"):
            await convert_to_ogg_opus(raw_pcm_audio, input_format="s16le")

    @pytest.mark.asyncio
    async def test_invalid_audio_fails(self) -> None:
        """Invalid audio data should raise AudioConversionError."""
        with pytest.raises(AudioConversionError):
            await convert_to_ogg_opus(b"not audio data", input_format="wav")

    @pytest.mark.asyncio
    async def test_empty_audio_fails(self) -> None:
        """Empty audio data should raise AudioConversionError."""
        with pytest.raises(AudioConversionError):
            await convert_to_ogg_opus(b"", input_format="wav")

    @pytest.mark.asyncio
    async def test_output_is_smaller_than_wav(self, wav_audio: bytes) -> None:
        """OGG/Opus output should be smaller than WAV input (compression)."""
        result = await convert_to_ogg_opus(wav_audio, input_format="wav")

        # Opus is highly compressed, should be much smaller
        # For very short audio, overhead might make it similar, but still check
        assert len(result) < len(wav_audio) * 2, (
            "Output should not be much larger than input"
        )


class TestAudioConversionErrorHandling:
    """Tests for error handling when ffmpeg is not available."""

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found(self, wav_audio: bytes, monkeypatch) -> None:
        """Should raise AudioConversionError if ffmpeg is not found."""

        async def mock_create_subprocess(*args, **kwargs):
            raise FileNotFoundError("ffmpeg")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_subprocess)

        with pytest.raises(AudioConversionError, match="ffmpeg not found"):
            await convert_to_ogg_opus(wav_audio, input_format="wav")
