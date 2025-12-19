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

from derp.common.sender import MessageSender
from derp.config import settings
from derp.llm.deps import AgentDeps
from derp.tools.wrapper import credit_aware_tool

TTS_MODEL = "gemini-2.5-pro-preview-tts"


def _filename_for_mime(mime: str) -> str:
    if "mpeg" in mime or "mp3" in mime:
        return "tts.mp3"
    if "ogg" in mime:
        return "tts.ogg"
    return "tts.wav"


async def generate_and_send_tts(
    deps: AgentDeps,
    *,
    text: str,
    model: str | None = None,
) -> None:
    tts_model = model or TTS_MODEL

    client = genai.Client(api_key=settings.google_api_paid_key)
    logfire.info("tts_start", model=tts_model, chars=len(text), chat_id=deps.chat_id)

    # Use async API
    response = await client.aio.models.generate_content(
        model=tts_model,
        contents=text,
        config=types.GenerateContentConfig(response_modalities=["AUDIO"]),
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

    sender = MessageSender.from_message(deps.message)
    await sender.reply_audio(
        audio_bytes,
        caption=text,
        filename=_filename_for_mime(audio_mime),
    )

    logfire.info(
        "tts_done",
        model=tts_model,
        bytes=len(audio_bytes),
        mime=audio_mime,
        chat_id=deps.chat_id,
    )


@credit_aware_tool("voice_tts")
async def voice_tts(
    ctx: RunContext[AgentDeps],
    text: str,
    *,
    model: str | None = None,
) -> str:
    """Generate speech audio from text and send it to the chat."""
    await generate_and_send_tts(ctx.deps, text=text, model=model)
    return "I've generated and sent the voice message."
