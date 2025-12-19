"""Image generation and editing handler using Pydantic-AI.

This handler processes /imagine and /edit commands with credit checking,
using the IMAGE tier model for image generation capabilities.

Credit-aware:
- Free tier: 1 free image per day
- Paid tier: Deducts credits per image
"""

from __future__ import annotations

import logfire
from aiogram import Router, flags
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _
from pydantic_ai import BinaryContent, BinaryImage
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from derp.common.extractor import Extractor
from derp.common.sender import MessageSender
from derp.credits import CreditService
from derp.filters.meta import MetaCommand, MetaInfo
from derp.llm import create_image_agent
from derp.models import Chat as ChatModel
from derp.models import User as UserModel

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

    sender = MessageSender.from_message(message)
    result = await sender.reply_photo(
        output.data,
        caption=caption,
        filename=_to_filename(output.media_type, 1),
    )
    return result if isinstance(result, Message) else result[-1]


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

    # Convert BinaryImages to MediaItems
    from derp.common.sender import MediaItem, MediaType

    media = [
        MediaItem(
            type=MediaType.PHOTO,
            data=image.data,
            filename=_to_filename(image.media_type, idx + 1),
        )
        for idx, image in enumerate(images)
    ]

    sender = MessageSender.from_message(message)
    sent_messages = await sender.send_media_group(
        media=media,
        caption=caption,
        reply_to=message,
    )
    return sent_messages[-1] if sent_messages else message


@router.message(MetaCommand("imagine", "image", "img", "и"))
@flags.chat_action(initial_sleep=2, action="upload_photo")
async def handle_imagine(
    message: Message,
    meta: MetaInfo,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Handle /imagine command for image generation.

    Credit-aware: checks credits/daily limit before generating.
    """
    prompt = meta.target_text
    if not prompt:
        return await message.reply(_("Usage: /imagine <prompt>"))

    if not user_model or not chat_model:
        return await message.reply(
            _("😅 Could not verify your access. Please try again.")
        )

    result = await credit_service.check_tool_access(
        user_model, chat_model, "image_generate"
    )

    if not result.allowed:
        return await message.reply(
            _(
                "✨ {reason}\n\n"
                "💡 Use /buy to get credits for unlimited image generation!"
            ).format(reason=result.reject_reason)
        )

    try:
        with logfire.span(
            "image_generate",
            _tags=["agent", "image"],
            telegram_chat_id=message.chat.id,
            telegram_user_id=message.from_user and message.from_user.id,
            prompt_length=len(prompt),
            credit_source=result.source,
        ):
            agent = create_image_agent()
            run_result = await agent.run(prompt)
            output = run_result.output

            if hasattr(run_result, "response") and hasattr(
                run_result.response, "images"
            ):
                images = run_result.response.images
                if images:
                    logfire.info("images_generated", count=len(images))
                    idempotency_key = (
                        f"imagine:{chat_model.telegram_id}:{message.message_id}"
                    )
                    await credit_service.deduct(
                        result,
                        user_model,
                        chat_model,
                        "image_generate",
                        idempotency_key=idempotency_key,
                    )
                    return await _send_multiple_images(meta.target_message, images)

            if isinstance(output, BinaryImage):
                idempotency_key = (
                    f"imagine:{chat_model.telegram_id}:{message.message_id}"
                )
                await credit_service.deduct(
                    result,
                    user_model,
                    chat_model,
                    "image_generate",
                    idempotency_key=idempotency_key,
                )

            return await _send_image_result(meta.target_message, output)

    except ModelHTTPError as exc:
        if exc.status_code == 429:
            logfire.warning(
                "imagine_rate_limited",
                status_code=exc.status_code,
                model=exc.model_name,
            )
            return await message.reply(
                _(
                    "⏳ The AI service is overloaded right now.\n\n"
                    "This happens during peak usage. Please wait 30-60 seconds "
                    "and try again."
                )
            )
        logfire.exception("imagine_model_http_error", status_code=exc.status_code)
        return await message.reply(
            _("😅 Something went wrong while generating the image. Try again later.")
        )
    except UnexpectedModelBehavior:
        logfire.warning("imagine_unexpected_behavior")
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


@router.message(MetaCommand("edit", "ed", "e", "е"))
@flags.chat_action(initial_sleep=2, action="upload_photo")
async def handle_edit(
    message: Message,
    meta: MetaInfo,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Handle /edit command for image editing.

    Credit-aware: checks credits/daily limit before editing.
    """
    prompt = meta.target_text
    if not prompt:
        return await message.reply(_("Reply to an image and use: /edit <prompt>"))

    photo = await Extractor.photo(message, with_profile_photo=True)
    if not photo:
        return await message.reply(
            _("Please reply to an image (photo/document/sticker) to edit it.")
        )

    if not user_model or not chat_model:
        return await message.reply(
            _("😅 Could not verify your access. Please try again.")
        )

    result = await credit_service.check_tool_access(
        user_model, chat_model, "image_generate"
    )

    if not result.allowed:
        return await message.reply(
            _(
                "✨ {reason}\n\n💡 Use /buy to get credits for unlimited image editing!"
            ).format(reason=result.reject_reason)
        )

    try:
        with logfire.span(
            "image_edit",
            _tags=["agent", "image"],
            telegram_chat_id=message.chat.id,
            telegram_user_id=message.from_user and message.from_user.id,
            prompt_length=len(prompt),
            credit_source=result.source,
        ):
            data = await photo.download()
            logfire.debug("source_image_downloaded", size=len(data))

            agent = create_image_agent()
            user_prompt: list[str | BinaryContent] = [
                prompt,
                BinaryContent(data=data, media_type=photo.media_type),
            ]

            run_result = await agent.run(user_prompt)
            output = run_result.output

            if hasattr(run_result, "response") and hasattr(
                run_result.response, "images"
            ):
                images = run_result.response.images
                if images:
                    logfire.info("images_edited", count=len(images))
                    idempotency_key = (
                        f"edit:{chat_model.telegram_id}:{message.message_id}"
                    )
                    await credit_service.deduct(
                        result,
                        user_model,
                        chat_model,
                        "image_generate",
                        idempotency_key=idempotency_key,
                    )
                    return await _send_multiple_images(meta.target_message, images)

            if isinstance(output, BinaryImage):
                idempotency_key = f"edit:{chat_model.telegram_id}:{message.message_id}"
                await credit_service.deduct(
                    result,
                    user_model,
                    chat_model,
                    "image_generate",
                    idempotency_key=idempotency_key,
                )

            return await _send_image_result(meta.target_message, output)

    except ModelHTTPError as exc:
        if exc.status_code == 429:
            logfire.warning(
                "edit_rate_limited",
                status_code=exc.status_code,
                model=exc.model_name,
            )
            return await message.reply(
                _(
                    "⏳ The AI service is overloaded right now.\n\n"
                    "This happens during peak usage. Please wait 30-60 seconds "
                    "and try again."
                )
            )
        logfire.exception("edit_model_http_error", status_code=exc.status_code)
        return await message.reply(
            _("😅 Something went wrong while editing the image. Try again later.")
        )
    except UnexpectedModelBehavior:
        logfire.warning("edit_unexpected_behavior")
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
