"""Text-to-speech tool for Pydantic-AI agents.

DEPRECATED: This module is kept for backwards compatibility.
Use derp.tools.gemini_tts instead.
"""

from derp.tools.gemini_tts import (
    GEMINI_TTS_SAMPLE_RATE,
    TTS_MODEL,
    generate_and_send_tts,
    voice_tts,
)

__all__ = ["TTS_MODEL", "GEMINI_TTS_SAMPLE_RATE", "generate_and_send_tts", "voice_tts"]
