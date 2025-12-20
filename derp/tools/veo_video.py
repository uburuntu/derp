"""Video generation tool for Pydantic-AI agents.

Implements Veo 3.1 video generation with optional reference image input.

Uses google-genai SDK because Pydantic-AI does not currently cover all Veo
capabilities.

Model details: https://ai.google.dev/gemini-api/docs/models.md.txt
Video docs: https://ai.google.dev/gemini-api/docs/video.md.txt
"""

from __future__ import annotations

import asyncio
from typing import Any

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


class VideoGenerationError(Exception):
    """Raised when video generation fails with specific details."""

    def __init__(
        self,
        message: str,
        *,
        operation_name: str | None = None,
        error_details: dict[str, Any] | None = None,
        rai_filtered_count: int | None = None,
        rai_filtered_reasons: list[str] | None = None,
    ):
        super().__init__(message)
        self.operation_name = operation_name
        self.error_details = error_details
        self.rai_filtered_count = rai_filtered_count
        self.rai_filtered_reasons = rai_filtered_reasons

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.rai_filtered_reasons:
            parts.append(f"RAI filtered: {', '.join(self.rai_filtered_reasons)}")
        if self.error_details:
            parts.append(f"Error: {self.error_details}")
        return " | ".join(parts)


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


async def _download_video_to_bytes(client: genai.Client, video: types.Video) -> bytes:
    """Download a generated video to memory using async client.

    The google-genai SDK populates video.video_bytes after download().
    """
    if video.video_bytes:
        return video.video_bytes

    return await client.aio.files.download(file=video)


async def generate_and_send_video(
    deps: AgentDeps,
    *,
    prompt: str,
    quality: str = "fast",
    duration_seconds: int = 6,
    aspect_ratio: str = "16:9",
    model: str | None = None,
    with_profile_photo: bool = False,
) -> None:
    """Generate and send a video to the chat.

    This is the shared implementation used by both:
    - /video command handler
    - agent tool call

    Args:
        deps: Agent dependencies with message context.
        prompt: Video generation prompt.
        quality: 'fast' or 'standard' (affects model selection).
        duration_seconds: Video length (6 or 8 seconds).
        aspect_ratio: Video aspect ratio ('16:9', '9:16', '1:1').
        model: Explicit model override (bypasses quality selection).
        with_profile_photo: If True, use user's profile photo as reference
            when no image is attached (for "animate my photo" requests).
    """
    veo_model = _pick_veo_model(quality=quality, model=model)

    # Use paid key for Veo (paid tier feature)
    client = genai.Client(api_key=settings.google_api_paid_key)

    # Extract reference image from message/reply, with optional profile photo fallback
    photo = await Extractor.photo(deps.message, with_profile_photo=with_profile_photo)
    image = (
        types.Image(image_bytes=await photo.download(), mime_type=photo.media_type)
        if photo
        else None
    )

    logfire.info(
        "veo_generate_start",
        model=veo_model,
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        has_reference_image=bool(photo),
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
        ),
    )

    logfire.debug(
        "veo_operation_started",
        operation_name=operation.name,
        chat_id=deps.chat_id,
    )

    poll_count = 0
    while not operation.done:
        poll_count += 1
        await asyncio.sleep(5)
        operation = await client.aio.operations.get(operation)
        logfire.debug(
            "veo_operation_poll",
            operation_name=operation.name,
            poll_count=poll_count,
            done=operation.done,
        )

    # Check for operation-level errors first
    if operation.error:
        raise VideoGenerationError(
            "Video generation operation failed",
            operation_name=operation.name,
            error_details=operation.error,
        )

    # Check response exists
    if not operation.response:
        raise VideoGenerationError(
            "Video generation completed but no response was returned",
            operation_name=operation.name,
        )

    response = operation.response

    # Log RAI filtering information if present
    if response.rai_media_filtered_count:
        logfire.warning(
            "veo_rai_filtered",
            operation_name=operation.name,
            filtered_count=response.rai_media_filtered_count,
            filtered_reasons=response.rai_media_filtered_reasons,
            chat_id=deps.chat_id,
        )

    # Check for generated videos
    generated = response.generated_videos
    if not generated:
        raise VideoGenerationError(
            "No videos were generated",
            operation_name=operation.name,
            rai_filtered_count=response.rai_media_filtered_count,
            rai_filtered_reasons=response.rai_media_filtered_reasons,
        )

    # Check the first video object
    video_obj = generated[0].video
    if not video_obj:
        raise VideoGenerationError(
            "Video generation returned empty video object",
            operation_name=operation.name,
            rai_filtered_count=response.rai_media_filtered_count,
            rai_filtered_reasons=response.rai_media_filtered_reasons,
        )
    video_bytes = await _download_video_to_bytes(client, video_obj)

    sender = MessageSender.from_message(deps.message)
    await sender.compose().text(prompt).video(video_bytes).reply()

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
    use_profile_photo: bool = False,
) -> str:
    """Generate a video using Veo 3.1 and send it to the chat.

    Args:
        prompt: Description of the video to generate. Be detailed and creative.
        quality: 'fast' for quick generation, 'standard' for higher quality.
        duration_seconds: Video length - 6 or 8 seconds.
        aspect_ratio: '16:9' (landscape), '9:16' (portrait/stories), or '1:1' (square).
        use_profile_photo: Set to True when the user asks to "use my photo",
            "animate my photo", "make a video of me", etc. This uses their
            Telegram profile picture as the reference image for video generation.
    """
    await generate_and_send_video(
        ctx.deps,
        prompt=prompt,
        quality=quality,
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        with_profile_photo=use_profile_photo,
    )
    return "[Sent directly to chat. Do not output anything else unless the user asked a follow-up question.]"
