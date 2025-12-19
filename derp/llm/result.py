"""Result wrapper for agent outputs.

AgentResult provides a unified interface for handling different
output types (text, images, mixed) and sending them as Telegram replies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiogram.exceptions
import logfire
from aiogram import md
from aiogram.types import BufferedInputFile, Message, ReactionTypeEmoji
from aiogram.utils.i18n import gettext as _
from aiogram.utils.media_group import MediaGroupBuilder
from pydantic_ai import BinaryImage, RunResult

if TYPE_CHECKING:
    pass


@dataclass
class AgentResult:
    """Unified result wrapper for agent outputs.

    Handles text, images, and mixed outputs, providing helper methods
    for formatting and sending as Telegram replies.
    """

    text: str | None = None
    images: list[BinaryImage] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)
    execution_results: list[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        """Check if the result has any content."""
        return bool(
            self.text or self.images or self.code_blocks or self.execution_results
        )

    @property
    def formatted_text(self) -> str:
        """Format all text content for Telegram."""
        parts: list[str] = []

        if self.text:
            parts.append(self.text)

        for code in self.code_blocks:
            parts.append(
                f"{md.bold(_('Generated Code:'))}\n{md.expandable_blockquote(code)}"
            )

        for result in self.execution_results:
            parts.append(f"{md.bold(_('Execution Result:'))}\n{md.blockquote(result)}")

        return "\n\n".join(parts)

    async def reply_to(
        self, message: Message, *, max_length: int = 4000
    ) -> Message | None:
        """Send the result as a reply to the given message.

        Args:
            message: The message to reply to.
            max_length: Maximum text length before truncation.

        Returns:
            The sent message, or None if nothing was sent.
        """
        if not self.has_content:
            # React with 👌 if no content to send
            try:
                await message.react(reaction=[ReactionTypeEmoji(emoji="👌")])
                logfire.debug("empty_response_reacted", message_id=message.message_id)
            except Exception:
                logfire.debug("react_failed", message_id=message.message_id)
            return None

        sent_message: Message | None = None

        # Send text if present
        text_response = self.formatted_text[:max_length]
        if text_response:
            sent_message = await self._send_text_safely(message, text_response)

        # Send images if present
        if self.images:
            sent_message = (
                await self._send_images(message, sent_message) or sent_message
            )

        return sent_message

    async def _send_text_safely(self, message: Message, text: str) -> Message:
        """Send text with markdown, falling back to plain text on parse errors."""
        try:
            return await message.reply(text, parse_mode="Markdown")
        except aiogram.exceptions.TelegramBadRequest as exc:
            if "can't parse entities" in exc.message:
                logfire.debug("markdown_parse_failed_fallback")
                return await message.reply(text, parse_mode=None)
            raise

    async def _send_images(
        self, original_message: Message, reply_to: Message | None = None
    ) -> Message | None:
        """Send images as photo messages or media groups."""
        if not self.images:
            return None

        target = reply_to or original_message

        try:
            # Single image: reply with photo
            if len(self.images) == 1:
                image = self.images[0]
                input_file = BufferedInputFile(
                    file=image.data,
                    filename=self._get_filename(image.media_type, 1),
                )
                return await target.reply_photo(photo=input_file)

            # Multiple images: send as media group(s)
            sent_messages: list[Message] = []
            start = 0
            idx = 1

            while start < len(self.images):
                chunk = self.images[start : start + 10]
                builder = MediaGroupBuilder()

                for image in chunk:
                    input_file = BufferedInputFile(
                        file=image.data,
                        filename=self._get_filename(image.media_type, idx),
                    )
                    builder.add_photo(media=input_file)
                    idx += 1

                msgs = await target.reply_media_group(media=builder.build())
                sent_messages.extend(msgs)
                start += 10

            logfire.info("images_sent", count=len(self.images))
            return sent_messages[-1] if sent_messages else None

        except Exception:
            logfire.warning("send_images_failed", _exc_info=True)
            if not reply_to:
                return await original_message.reply(
                    _("📊 Generated images, but couldn't display them.")
                )
            return None

    @staticmethod
    def _get_filename(mime_type: str, idx: int) -> str:
        """Generate a filename based on mime type."""
        ext = "png" if "png" in mime_type else "jpg"
        return f"generated_{idx}.{ext}"

    @classmethod
    def from_run_result(cls, result: RunResult) -> AgentResult:
        """Create an AgentResult from a Pydantic-AI RunResult.

        Handles both text and BinaryImage outputs.
        """
        text: str | None = None
        images: list[BinaryImage] = []

        output = result.output

        # Handle union types (BinaryImage | str)
        if isinstance(output, BinaryImage):
            images.append(output)
            # Check if there's also text in the response
            if hasattr(result, "response") and hasattr(result.response, "text"):
                text = result.response.text
        elif isinstance(output, str):
            text = output
        elif isinstance(output, list):
            # Handle list outputs (e.g., multiple images)
            for item in output:
                if isinstance(item, BinaryImage):
                    images.append(item)
                elif isinstance(item, str):
                    text = (text or "") + item

        # Also check for images in the response object
        if hasattr(result, "response") and hasattr(result.response, "images"):
            for img in result.response.images:
                if isinstance(img, BinaryImage) and img not in images:
                    images.append(img)

        return cls(text=text, images=images)
