"""Text-to-speech tool for Pydantic-AI agents.

Uses google-genai SDK for audio output.

Models: https://ai.google.dev/gemini-api/docs/models.md.txt
Pricing: https://ai.google.dev/gemini-api/docs/pricing.md.txt
"""

from __future__ import annotations

import logfire
from google import genai
from google.genai import types
from pydantic_ai import RunContext

from derp.catalog import AudioPricing, GoogleModelKey, GoogleModelSpec, get_google_model
from derp.common.audio import convert_to_ogg_opus
from derp.common.sender import MessageSender
from derp.config import settings
from derp.credits.tools import TTS_MAX_OUTPUT_SECONDS
from derp.llm.deps import AgentDeps
from derp.tools.wrapper import credit_aware_tool

# Gemini TTS returns raw 16-bit PCM at 24kHz
GEMINI_TTS_SAMPLE_RATE = 24000


async def generate_and_send_tts(
    deps: AgentDeps,
    *,
    text: str,
    model: GoogleModelSpec | None = None,
) -> None:
    spec = model or get_google_model(GoogleModelKey.TTS)
    pricing = spec.pricing
    if not isinstance(pricing, AudioPricing):
        raise ValueError(f"{spec.key} is not an audio model")
    max_output_tokens = pricing.audio_tokens_per_second * TTS_MAX_OUTPUT_SECONDS

    client = genai.Client(api_key=settings.google_api_paid_key.get_secret_value())
    logfire.info(
        "tts_start",
        model=spec.provider_model_id,
        chars=len(text),
        chat_id=deps.chat_id,
    )

    # Use async API
    response = await client.aio.models.generate_content(
        model=spec.provider_model_id,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            max_output_tokens=max_output_tokens,
        ),
    )

    audio_bytes: bytes | None = None
    audio_mime: str | None = None

    if response.parts:
        for part in response.parts:
            if part.inline_data:
                audio_bytes = part.inline_data.data
                audio_mime = part.inline_data.mime_type
                break

    if not audio_bytes or not audio_mime:
        raise RuntimeError("No audio returned from TTS model.")

    # Convert to OGG/Opus for Telegram voice messages
    # Gemini returns audio/L16;rate=24000 (raw 16-bit PCM)
    mime_lower = audio_mime.lower()
    if "l16" in mime_lower or "pcm" in mime_lower:
        ogg_bytes = await convert_to_ogg_opus(
            audio_bytes,
            input_format="s16le",
            sample_rate=GEMINI_TTS_SAMPLE_RATE,
        )
    else:
        # Fallback: assume WAV container
        ogg_bytes = await convert_to_ogg_opus(audio_bytes, input_format="wav")

    sender = MessageSender.from_message(deps.message)
    await sender.compose().voice(ogg_bytes).reply()

    logfire.info(
        "tts_done",
        model=spec.provider_model_id,
        input_bytes=len(audio_bytes),
        output_bytes=len(ogg_bytes),
        input_mime=audio_mime,
        chat_id=deps.chat_id,
    )


@credit_aware_tool("voice_tts")
async def voice_tts(
    ctx: RunContext[AgentDeps],
    text: str,
) -> str:
    """Generate speech audio from text and send it to the chat.

    Use this tool when the user asks you to say something out loud,
    read text aloud, or create a voice message.

    Args:
        text: Text for a voice message of up to about 30 seconds. Split longer
            narration into separate requests.
    """
    await generate_and_send_tts(ctx.deps, text=text)
    return "[Sent directly to chat. Do not output anything else unless the user asked a follow-up question.]"
