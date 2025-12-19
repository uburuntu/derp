"""Image generation and editing handler using Pydantic-AI.

This handler processes /imagine and /edit commands for premium users,
using the IMAGE tier model for image generation capabilities.
"""

from __future__ import annotations

import logfire
from aiogram import F, Router, flags
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.i18n import gettext as _
from aiogram.utils.media_group import MediaGroupBuilder
from pydantic_ai import BinaryContent, BinaryImage
from pydantic_ai.exceptions import UnexpectedModelBehavior

from derp.common.extractor import Extractor
from derp.config import settings
from derp.filters.meta import MetaCommand, MetaInfo
from derp.llm import create_image_agent

router = Router(name="image")


def _to_filename(mime: str, idx: int) -> str:
    """Generate a filename based on mime type."""
    ext = "jpg" if mime.endswith(("jpeg", "jpg")) else "png"
    return f"generated_{idx}.{ext}"


async def _send_image_result(
    message: Message,
    output: BinaryImage | str,
    *,
    caption: str | None = None,
) -> Message:
    """Send an image result or text fallback."""
    if isinstance(output, str):
        # Text response (refusal or error message from model)
        return await message.reply(output or _("🤷 No image generated."))

    # Single image
    input_file = BufferedInputFile(
        file=output.data,
        filename=_to_filename(output.media_type, 1),
    )
    return await message.reply_photo(photo=input_file, caption=caption)


async def _send_multiple_images(
    message: Message,
    images: list[BinaryImage],
    *,
    caption: str | None = None,
) -> Message:
    """Send multiple images as a media group."""
    if not images:
        return await message.reply(_("🤷 No images generated."))

    if len(images) == 1:
        return await _send_image_result(message, images[0], caption=caption)

    # Multiple images: send as media group(s)
    sent_messages: list[Message] = []
    start = 0
    idx = 1

    while start < len(images):
        chunk = images[start : start + 10]
        builder = MediaGroupBuilder(caption=caption if start == 0 else None)

        for image in chunk:
            input_file = BufferedInputFile(
                file=image.data,
                filename=_to_filename(image.media_type, idx),
            )
            builder.add_photo(media=input_file)
            idx += 1

        msgs = await message.reply_media_group(media=builder.build())
        sent_messages.extend(msgs)
        start += 10

    return sent_messages[-1]


@router.message(
    F.chat.id.in_(settings.premium_chat_ids)
    | F.from_user.id.in_(settings.premium_chat_ids),
    MetaCommand("imagine", "image", "img", "и"),
)
@flags.chat_action(initial_sleep=2, action="upload_photo")
async def handle_imagine(message: Message, meta: MetaInfo) -> Message:
    """Handle /imagine command for image generation."""
    prompt = meta.target_text
    if not prompt:
        return await message.reply(_("Usage: /imagine <prompt>"))

    try:
        with logfire.span(
            "image_generate",
            _tags=["agent", "image"],
            telegram_chat_id=message.chat.id,
            telegram_user_id=message.from_user and message.from_user.id,
            prompt_length=len(prompt),
        ):
            agent = create_image_agent()
            result = await agent.run(prompt)

            # Handle the output (BinaryImage or str)
            output = result.output

            # Check for images in response object as well
            if hasattr(result, "response") and hasattr(result.response, "images"):
                images = result.response.images
                if images:
                    logfire.info("images_generated", count=len(images))
                    return await _send_multiple_images(meta.target_message, images)

            return await _send_image_result(meta.target_message, output)

    except UnexpectedModelBehavior:
        logfire.warning("imagine_rate_limited")
        return await message.reply(
            _(
                "⏳ I'm getting too many requests right now. "
                "Please try again in about 30 seconds."
            )
        )
    except Exception:
        logfire.exception("imagine_failed")
        return await message.reply(
            _("😅 Something went wrong while generating the image. Try again later.")
        )


@router.message(
    F.chat.id.in_(settings.premium_chat_ids)
    | F.from_user.id.in_(settings.premium_chat_ids),
    MetaCommand("edit", "ed", "e", "е"),
)
@flags.chat_action(initial_sleep=2, action="upload_photo")
async def handle_edit(message: Message, meta: MetaInfo) -> Message:
    """Handle /edit command for image editing."""
    prompt = meta.target_text
    if not prompt:
        return await message.reply(_("Reply to an image and use: /edit <prompt>"))

    photo = await Extractor.photo(message, with_profile_photo=True)
    if not photo:
        return await message.reply(
            _("Please reply to an image (photo/document/sticker) to edit it.")
        )

    try:
        with logfire.span(
            "image_edit",
            _tags=["agent", "image"],
            telegram_chat_id=message.chat.id,
            telegram_user_id=message.from_user and message.from_user.id,
            prompt_length=len(prompt),
        ):
            data = await photo.download()
            logfire.debug("source_image_downloaded", size=len(data))

            agent = create_image_agent()

            # Build prompt with image
            user_prompt: list[str | BinaryContent] = [
                prompt,
                BinaryContent(data=data, media_type=photo.media_type),
            ]

            result = await agent.run(user_prompt)

            # Handle the output
            output = result.output

            # Check for images in response object
            if hasattr(result, "response") and hasattr(result.response, "images"):
                images = result.response.images
                if images:
                    logfire.info("images_edited", count=len(images))
                    return await _send_multiple_images(meta.target_message, images)

            return await _send_image_result(meta.target_message, output)

    except UnexpectedModelBehavior:
        logfire.warning("edit_rate_limited")
        return await message.reply(
            _(
                "⏳ I'm getting too many requests right now. "
                "Please try again in about 30 seconds."
            )
        )
    except Exception:
        logfire.exception("edit_failed")
        return await message.reply(
            _("😅 Something went wrong while editing the image. Try again later.")
        )


@router.message(MetaCommand("imagine"))
@router.message(MetaCommand("edit"))
async def handle_non_premium(message: Message) -> Message:
    """Handle image commands from non-premium users."""
    return await message.reply(
        _("🔒 This feature is only available for premium users and chats.")
    )
