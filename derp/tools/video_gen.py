"""Video generation tool for Pydantic-AI agents.

Implements Veo 3.1 video generation with optional reference image input.

Uses google-genai SDK because Pydantic-AI does not currently cover all Veo
capabilities.

Model details: https://ai.google.dev/gemini-api/docs/models.md.txt
Video docs: https://ai.google.dev/gemini-api/docs/video.md.txt
"""

from __future__ import annotations

import asyncio
import tempfile

import logfire
from google import genai
from google.genai import types
from pydantic_ai import RunContext

from derp.common.extractor import Extractor
from derp.common.sender import MessageSender
from derp.config import settings
from derp.llm.deps import AgentDeps
from derp.tools.wrapper import credit_aware_tool

VEO_31_FAST = "veo-3.1-fast-generate-preview"
VEO_31_STANDARD = "veo-3.1-generate-preview"


def _pick_veo_model(*, quality: str, model: str | None) -> str:
    """Pick a Veo model id.

    Args:
        quality: 'fast' or 'standard'
        model: explicit model override (used by tooling)
    """
    if model:
        return model
    if quality.lower() == "standard":
        return VEO_31_STANDARD
    return VEO_31_FAST


async def _extract_reference_image_bytes(deps: AgentDeps) -> tuple[bytes, str] | None:
    """Extract a reference image from the current message or reply."""
    msg = deps.message
    photo = await Extractor.photo(msg)
    if not photo and msg.reply_to_message:
        photo = await Extractor.photo(msg.reply_to_message)
    if not photo:
        return None
    data = await photo.download()
    mime = photo.media_type or "image/jpeg"
    return (data, mime)


async def _download_video_to_bytes(client: genai.Client, video: object) -> bytes:
    """Download a generated video to memory using async client."""
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        # Download directly to file path using async client
        await client.aio.files.download(file=video, path=tmp.name)
        tmp.seek(0)
        return tmp.read()


async def generate_and_send_video(
    deps: AgentDeps,
    *,
    prompt: str,
    quality: str = "fast",
    duration_seconds: int = 6,
    aspect_ratio: str = "16:9",
    enhance_prompt: bool = True,
    model: str | None = None,
) -> None:
    """Generate and send a video to the chat.

    This is the shared implementation used by both:
    - /video command handler
    - agent tool call
    """
    veo_model = _pick_veo_model(quality=quality, model=model)

    # Use paid key for Veo (paid tier feature)
    client = genai.Client(api_key=settings.google_api_paid_key)

    ref = await _extract_reference_image_bytes(deps)
    image = types.Image(image_bytes=ref[0]) if ref else None

    logfire.info(
        "veo_generate_start",
        model=veo_model,
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        has_reference_image=bool(ref),
        chat_id=deps.chat_id,
    )

    # Use async API
    operation = await client.aio.models.generate_videos(
        model=veo_model,
        prompt=prompt,
        image=image,
        config=types.GenerateVideosConfig(
            number_of_videos=1,
            duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio,
            enhance_prompt=enhance_prompt,
        ),
    )

    while not operation.done:
        await asyncio.sleep(5)
        operation = await client.aio.operations.get(operation)

    generated = operation.response.generated_videos
    if not generated:
        raise RuntimeError("No videos were generated.")

    video_obj = generated[0].video
    video_bytes = await _download_video_to_bytes(client, video_obj)

    sender = MessageSender.from_message(deps.message)
    await sender.reply_video(video_bytes, caption=prompt)

    logfire.info(
        "veo_generate_done",
        model=veo_model,
        bytes=len(video_bytes),
        chat_id=deps.chat_id,
    )


@credit_aware_tool("video_generate")
async def video_generate(
    ctx: RunContext[AgentDeps],
    prompt: str,
    *,
    quality: str = "fast",
    duration_seconds: int = 6,
    aspect_ratio: str = "16:9",
    enhance_prompt: bool = True,
    model: str | None = None,
) -> str:
    """Generate a video using Veo 3.1 and send it to the chat."""
    await generate_and_send_video(
        ctx.deps,
        prompt=prompt,
        quality=quality,
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        enhance_prompt=enhance_prompt,
        model=model,
    )
    return "I've generated and sent the video."
