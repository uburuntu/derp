"""AI-powered response handler using Google's native genai library for Gemini models only."""

from __future__ import annotations

import json
from typing import Any

import aiogram.exceptions
import logfire
from aiogram import F, Router, flags, md
from aiogram.filters import Command
from aiogram.handlers import MessageHandler
from aiogram.types import (
    BufferedInputFile,
    Message,
    ReactionTypeEmoji,
)
from aiogram.utils.i18n import gettext as _

from derp.common.extractor import Extractor
from derp.common.llm_gemini import Gemini, GeminiResult
from derp.config import settings
from derp.db import get_db_manager, get_recent_messages
from derp.filters import DerpMentionFilter
from derp.models import Chat as ChatModel

router = Router(name="gemini")


@logfire.instrument()
async def extract_media_for_gemini(message: Message) -> list[dict[str, Any]]:
    """Extract supported media from message for Gemini processing."""
    media_parts: list[dict[str, Any]] = []

    # Extract photo (includes image documents and static stickers)
    if photo := await Extractor.photo(message):
        try:
            image_data = await photo.download()
            media_parts.append(
                {
                    "data": image_data,
                    "mime_type": photo.media_type or "image/jpeg",
                }
            )
        except Exception:
            logfire.exception("photo_download_failed")

    # Extract video (includes video stickers, animations, video notes)
    if video := await Extractor.video(message):
        try:
            video_data = await video.download()
            media_parts.append(
                {
                    "data": video_data,
                    "mime_type": video.media_type or "video/mp4",
                }
            )
        except Exception:
            logfire.exception("video_download_failed")

    # Extract audio (includes audio files and voice messages)
    if audio := await Extractor.audio(message):
        try:
            audio_data = await audio.download()
            media_parts.append(
                {
                    "data": audio_data,
                    "mime_type": audio.media_type or "audio/ogg",
                }
            )
        except Exception:
            logfire.exception("audio_download_failed")

    # Extract document (includes PDF, Word, Excel, etc.)
    if (
        document := await Extractor.document(message)
    ) and document.media_type == "application/pdf":
        try:
            document_data = await document.download()
            media_parts.append(
                {"data": document_data, "mime_type": document.media_type}
            )
        except Exception:
            logfire.exception("document_download_failed")

    return media_parts


async def _build_context(message: Message, chat_settings: ChatModel | None) -> str:
    """Build the exact context Derp uses for Gemini.

    This function is reused by both /context and the Gemini handler.
    """
    context_parts: list[str] = []

    context_parts.extend(
        [
            "# CHAT",
            json.dumps(
                message.chat.model_dump(
                    exclude_defaults=True, exclude_none=True, exclude_unset=True
                )
            ),
        ]
    )

    if chat_settings and chat_settings.llm_memory:
        context_parts.extend(["# CHAT MEMORY", chat_settings.llm_memory])

    # Recent chat history from messages table
    db = get_db_manager()
    async with db.read_session() as session:
        recent_msgs = await get_recent_messages(
            session, chat_telegram_id=message.chat.id, limit=100
        )

    if recent_msgs:
        context_parts.append("# RECENT CHAT HISTORY")
        context_parts.extend(
            json.dumps(
                {
                    "message_id": m.telegram_message_id,
                    "sender": m.user
                    and {
                        "user_id": m.user.telegram_id,
                        "name": m.user.display_name,
                        "username": m.user.username,
                    },
                    "date": m.telegram_date and m.telegram_date.isoformat(),
                    "content": m.content_type,
                    "text": m.text,
                    "reply_to": m.reply_to_message_id,
                    "attachment": m.attachment_type,
                },
                ensure_ascii=False,
            )
            for m in recent_msgs
        )

    # Current message
    context_parts.extend(
        [
            "# CURRENT MESSAGE",
            message.model_dump_json(
                exclude_defaults=True, exclude_none=True, exclude_unset=True
            ),
        ]
    )

    return "\n".join(context_parts)


@router.message(Command("context"), F.from_user.id.in_(settings.admin_ids))
async def show_context(message: Message, chat_settings: ChatModel | None) -> None:
    ctx = await _build_context(message, chat_settings)
    stats = f"Context length: {len(ctx)} characters, included {ctx.count('message_id')} messages"
    await message.reply(stats)


@router.message(DerpMentionFilter())
@router.message(Command("derp"))
@router.message(F.chat.type == "private")
@router.message(F.reply_to_message.from_user.id == settings.bot_id)
class GeminiResponseHandler(MessageHandler):
    """Class-based message handler for AI responses using Google's native Gemini API."""

    @property
    def gemini(self) -> Gemini:
        """Get the Gemini service instance."""
        return Gemini()

    async def _generate_context(self, message: Message) -> str:
        chat_settings: ChatModel | None = self.data.get("chat_settings")
        return await _build_context(message, chat_settings)

    def _format_response_text(self, result: GeminiResult) -> str:
        """Format response data into a cohesive text message."""
        parts = []

        # Add main text content
        if result.text_parts:
            parts.extend(result.text_parts)

        # Add code blocks
        for code in result.code_blocks:
            parts.append(
                f"{md.bold('Generated Code:')}\n{md.expandable_blockquote(code)}"
            )

        # Add execution results
        for res in result.execution_results:
            parts.append(f"{md.bold('Execution Result:')}\n{md.blockquote(res)}")

        return "\n\n".join(parts) if parts else ""

    async def _send_text_safely(self, text: str) -> Message:
        """Send text with markdown, falling back to quoted text if parsing fails."""
        try:
            return await self.event.reply(text, parse_mode="Markdown")
        except aiogram.exceptions.TelegramBadRequest as exc:
            if "can't parse entities" in exc.message:
                return await self.event.reply(text, parse_mode=None)
            raise

    async def _send_image(
        self, image_data: dict[str, Any], reply_to: Message | None = None
    ) -> Message | None:
        """Send an image from Gemini's code execution."""
        try:
            file_extension = "png" if "png" in image_data["mime_type"] else "jpg"
            filename = f"generated_graph.{file_extension}"

            input_file = BufferedInputFile(file=image_data["data"], filename=filename)

            if reply_to:
                return await reply_to.reply_photo(
                    photo=input_file, caption="Generated visualization"
                )
            else:
                return await self.event.reply_photo(
                    photo=input_file, caption="Generated visualization"
                )
        except Exception:
            logfire.warning("send_generated_image_failed", _exc_info=True)
            if not reply_to:
                return await self.event.reply(
                    "📊 Generated a visualization, but couldn't display it."
                )
        return None

    @flags.chat_action
    async def handle(self) -> Any:
        """Handle messages using Gemini API."""
        try:
            with logfire.span("gemini_generate") as span:
                # Set Telegram context using semantic conventions
                span.set_attribute("telegram.chat_id", self.event.chat.id)
                span.set_attribute(
                    "telegram.user_id", self.event.from_user and self.event.from_user.id
                )
                span.set_attribute("telegram.message_id", self.event.message_id)
                span.set_attribute("gen_ai.request.model", settings.default_llm_model)

                # Build request
                request = (
                    self.gemini.create_request().with_google_search().with_url_context()
                )

                context = await self._generate_context(self.event)
                request.with_text(context)

                # Enrich span with context stats
                context_messages = context.count('"message_id"')
                span.set_attribute("derp.context_chars", len(context))
                span.set_attribute("derp.context_messages", context_messages)

                media_parts = await extract_media_for_gemini(self.event)
                for media in media_parts:
                    request.with_media(media["data"], media["mime_type"])

                span.set_attribute("derp.has_media", len(media_parts) > 0)
                span.set_attribute("derp.media_count", len(media_parts))

                # Execute request (LLM call is auto-instrumented)
                final_response = await request.execute()

                # Enrich span with response stats
                span.set_attribute(
                    "derp.response_has_text", bool(final_response.text_parts)
                )
                span.set_attribute(
                    "derp.response_code_blocks", len(final_response.code_blocks)
                )
                span.set_attribute("derp.response_images", len(final_response.images))

                # Check if we have any content to send
                if not final_response.has_content:
                    return await self.event.react(
                        reaction=[ReactionTypeEmoji(emoji="👌")]
                    )

                # Send the complete response
                text_response = self._format_response_text(final_response)[:4000]

                sent_message = None
                if text_response:
                    sent_message = await self._send_text_safely(text_response)

                for image_data in final_response.images:
                    sent_message = (
                        await self._send_image(image_data, sent_message) or sent_message
                    )

                return sent_message

        except Exception:
            logfire.exception("gemini_handler_failed")
            return await self.event.reply(
                _(
                    "😅 Something went wrong with Gemini. I couldn't process that message."
                )
            )
