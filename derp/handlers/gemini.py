"""AI-powered response handler using Google's native genai library for Gemini models only."""

from __future__ import annotations

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
    Update,
)
from aiogram.utils.i18n import gettext as _

from ..common.database import get_database_client
from ..common.llm_gemini import Gemini, GeminiResult
from ..common.tg import Extractor
from ..config import settings
from ..filters import DerpMentionFilter
from ..queries.chat_settings_async_edgeql import ChatSettingsResult
from ..queries.select_active_updates_async_edgeql import select_active_updates

router = Router(name="gemini")


@logfire.instrument()
async def extract_media_for_gemini(message: Message) -> list[dict[str, Any]]:
    """Extract supported media from message for Gemini processing."""
    media_parts: list[dict[str, Any]] = []

    # Extract photo (includes image documents and static stickers)
    if photo := Extractor.photo(message):
        try:
            image_data = await photo.download()
            media_parts.append(
                {
                    "data": image_data,
                    "mime_type": photo.media_type or "image/jpeg",
                }
            )
            logfire.info(f"Extracted photo from message {photo.message.message_id}")
        except Exception as e:
            logfire.warning(f"Failed to download photo: {e}")

    # Extract video (includes video stickers, animations, video notes)
    if video := Extractor.video(message):
        try:
            video_data = await video.download()
            media_parts.append(
                {
                    "data": video_data,
                    "mime_type": video.media_type or "video/mp4",
                }
            )
            logfire.info(
                f"Extracted video from message {video.message.message_id}, duration: {video.duration}s"
            )
        except Exception as e:
            logfire.warning(f"Failed to download video: {e}")

    # Extract audio (includes audio files and voice messages)
    if audio := Extractor.audio(message):
        try:
            audio_data = await audio.download()
            media_parts.append(
                {
                    "data": audio_data,
                    "mime_type": audio.media_type or "audio/ogg",
                }
            )
            logfire.info(
                f"Extracted audio from message {audio.message.message_id}, duration: {audio.duration}s"
            )
        except Exception as e:
            logfire.warning(f"Failed to download audio: {e}")

    # Extract document (includes PDF, Word, Excel, etc.)
    if (
        document := Extractor.document(message)
    ) and document.media_type == "application/pdf":
        try:
            document_data = await document.download()
            media_parts.append(
                {"data": document_data, "mime_type": document.media_type}
            )
            logfire.info(
                f"Extracted document from message {document.message.message_id}"
            )
        except Exception as e:
            logfire.warning(f"Failed to download document: {e}")

    return media_parts


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
        """Generate context for the AI prompt from the message and recent chat history."""
        chat_settings: ChatSettingsResult | None = self.data.get("chat_settings")
        context_parts: list[str] = []

        if chat_settings and chat_settings.llm_memory:
            context_parts.extend(
                [
                    "--- Chat Memory ---",
                    chat_settings.llm_memory,
                ]
            )

        # Add recent chat history
        db_client = get_database_client()
        async with db_client.get_executor() as executor:
            recent_updates = await select_active_updates(
                executor, chat_id=message.chat.id, limit=15
            )

        if recent_updates:
            context_parts.append("--- Recent Chat History ---")
            context_parts.extend(
                f"Update: {update.raw_data}" for update in recent_updates
            )

        # Add current message
        context_parts.extend(
            [
                "--- Current Message ---",
                message.model_dump_json(
                    exclude_defaults=True, exclude_none=True, exclude_unset=True
                ),
            ]
        )

        return "\n".join(context_parts)

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
        except Exception as e:
            logfire.warning(f"Failed to send generated image: {e}")
            if not reply_to:
                return await self.event.reply(
                    "📊 Generated a visualization, but couldn't display it."
                )
        return None

    @flags.chat_action
    async def handle(self) -> Any:
        """Handle messages using Gemini API."""
        try:
            # Create tool dependencies
            # chat_settings: ChatSettingsResult | None = self.data.get("chat_settings")
            # deps = ToolDeps(
            #     message=self.event,
            #     chat_settings=chat_settings,
            #     db_client=get_database_client(),
            # )

            # Build request
            request = (
                self.gemini.create_request()
                .with_google_search()
                .with_url_context()
                # .with_tool(update_chat_memory, deps) # Uncomment to enable memory
            )

            context = await self._generate_context(self.event)
            request.with_text(context)

            media_parts = await extract_media_for_gemini(self.event)
            for media in media_parts:
                request.with_media(media["data"], media["mime_type"])

            # Execute request
            final_response = await request.execute()

            # Check if we have any content to send
            if not final_response.has_content:
                return await self.event.react(reaction=[ReactionTypeEmoji(emoji="👌")])

            # Send the complete response
            text_response = self._format_response_text(final_response)[:4000]

            sent_message = None
            if text_response:
                sent_message = await self._send_text_safely(text_response)

            for image_data in final_response.images:
                sent_message = (
                    await self._send_image(image_data, sent_message) or sent_message
                )

            if sent_message:
                # Wait for the middleware's database task to complete first to avoid race conditions
                try: 
                    if "db_task" in self.data:
                        await self.data["db_task"]
                except Exception as e:
                    logfire.exception("Failed to complete database task")

                # Store bot's response in database
                try:
                    db_client = get_database_client()
                    update = Update.model_validate(
                        {"update_id": 0, "message": sent_message}
                    )
                    await db_client.create_bot_update_with_upserts(
                        update_id=0,
                        update_type="message",
                        raw_data=update.model_dump(
                            exclude_none=True, exclude_defaults=True
                        ),
                        user=sent_message.from_user,
                        chat=sent_message.chat,
                        sender_chat=None,
                    )
                except Exception as e:
                    logfire.exception("Failed to store bot response in database")

            return sent_message

        except Exception:
            logfire.exception("Error in Gemini response handler")
            return await self.event.reply(
                _(
                    "😅 Something went wrong with Gemini. I couldn't process that message."
                )
            )
