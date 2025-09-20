from __future__ import annotations

import base64

import logfire
from aiogram import F, Router, flags
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.i18n import gettext as _
from aiogram.utils.media_group import MediaGroupBuilder
from google import genai
from google.genai import types
from google.genai.types import GenerateContentResponse

from ..common.meta import MetaCommand, MetaInfo
from ..common.tg import Extractor
from ..config import settings

router = Router(name="gemini_image")


def _get_genai_client() -> genai.Client:
    api_key = settings.google_api_paid_key
    if not api_key:
        raise RuntimeError("Google API key is required for image generation")
    return genai.Client(api_key=api_key)


def _extract_images(response: GenerateContentResponse) -> list[tuple[bytes, str]]:
    """Return a list of (data, mime_type) tuples from inline image parts."""
    images: list[tuple[bytes, str]] = []
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return images
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            mime = getattr(inline, "mime_type", "") if inline else ""
            if inline and mime.startswith("image/"):
                data = getattr(inline, "data", b"")
                if isinstance(data, str):
                    try:
                        data = base64.b64decode(data)
                    except Exception:
                        continue
                if isinstance(data, (bytes, bytearray)):
                    images.append((bytes(data), mime))
    except Exception:
        logfire.exception("Failed to extract images from Gemini response")
    return images


def _extract_first_text(response: GenerateContentResponse) -> str | None:
    """Return the first text part from the response, if any."""
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                return text
    except Exception:
        logfire.exception("Failed to extract text from Gemini response")
    return None


def _to_filename(mime: str, idx: int) -> str:
    ext = (
        "png"
        if mime.endswith("png")
        else "jpg" if mime.endswith(("jpeg", "jpg")) else "png"
    )
    return f"gemini_image_{idx}.{ext}"


async def _send_images(
    message: Message, response: GenerateContentResponse, *, caption: str | None = None
) -> Message:
    """Send all images as a media group when possible; fall back to text."""
    images = _extract_images(response)
    if not images:
        text = _extract_first_text(response) or _("🤷 No image generated.")
        return await message.reply(text)

    # Single image: reply with a photo
    if len(images) == 1:
        data, mime = images[0]
        input_file = BufferedInputFile(file=data, filename=_to_filename(mime, 1))
        return await message.reply_photo(photo=input_file, caption=caption)

    # 2-10 images: send as a media group; if >10, chunk into groups of 10
    sent_messages: list[Message] = []
    start = 0
    idx = 1
    while start < len(images):
        chunk = images[start : start + 10]
        builder = MediaGroupBuilder(caption=caption if start == 0 else None)
        for data, mime in chunk:
            input_file = BufferedInputFile(file=data, filename=_to_filename(mime, idx))
            builder.add_photo(media=input_file)
            idx += 1

        msgs = await message.reply_media_group(
            media=builder.build(),
        )
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
    prompt = meta.target_text
    if not prompt:
        return await message.reply(_("Usage: /imagine <prompt>"))

    try:
        client = _get_genai_client()
        contents = [
            types.Part.from_text(text=prompt),
        ]
        response = client.models.generate_content(
            model="gemini-2.5-flash-image-preview",
            contents=contents,
        )
        return await _send_images(meta.target_message, response)
    except Exception:
        logfire.exception("Error generating image with Gemini")
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
    prompt = meta.target_text
    if not prompt:
        return await message.reply(_("Reply to an image and use: /edit <prompt>"))

    photo = await Extractor.photo(message, with_profile_photo=True)
    if not photo:
        return await message.reply(
            _("Please reply to an image (photo/document sticker) to edit it.")
        )

    try:
        data = await photo.download()

        client = _get_genai_client()
        contents = [
            types.Part.from_text(text=prompt),
            types.Part.from_bytes(data=data, mime_type=photo.media_type),
        ]
        response = client.models.generate_content(
            model="gemini-2.5-flash-image-preview",
            contents=contents,
        )
        return await _send_images(meta.target_message, response)
    except Exception:
        logfire.exception("Error editing image with Gemini")
        return await message.reply(
            _("😅 Something went wrong while editing the image. Try again later.")
        )


@router.message(MetaCommand("imagine"))
@router.message(MetaCommand("edit"))
async def handle_non_premium(message: Message) -> Message:
    return await message.reply(
        _("🔒 This feature is only available for premium users and chats.")
    )
