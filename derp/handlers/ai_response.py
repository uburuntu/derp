"""AI-powered response handler for messages mentioning 'derp' or 'дерп' and /derp command."""

import re
from functools import cached_property
from typing import Any

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, or_f
from aiogram.handlers import MessageHandler
from aiogram.types import Chat, Message, User
from aiogram.utils.i18n import gettext as _
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from ..common.utils import get_logger
from ..config import settings

router = Router(name="ai")
logger = get_logger("AIResponse")


class DerpMentionFilter(BaseFilter):
    """Filter for messages that mention 'derp' or 'дерп'."""

    async def __call__(self, message: Message) -> bool:
        if not message.text:
            return False

        # Case-insensitive check for "derp" in English or Russian
        pattern = r"\b(derp|дерп)\b"
        return bool(re.search(pattern, message.text, re.IGNORECASE))


class AIService:
    """Service class for handling AI operations. Reusable for future agentic tools."""

    def __init__(self):
        self._system_prompt = (
            "You are a helpful, conversational assistant in Telegram, called Derp. "
            "Reply in the user's language, using Markdown (bold, italic, underline, strikethrough, code, link only). "
            "Be concise, friendly, and clear—responses under 200 words unless more detail is requested. "
            "Personalize replies using user and chat context, when needed. "
            "Transliterate names to match the user's language (e.g., Ramzan → Рамзан). "
            "If users are ironic or joking, you can be a bit sarcastic or agressive. "
            "You can't see the sent media yet. "
            "Don't finish your response with follow up questions like `If you need more details, let me know`. "
        )

    @cached_property
    def agent(self) -> Agent:
        model = OpenAIModel(
            "o3-mini", provider=OpenAIProvider(api_key=settings.openai_api_key)
        )
        return Agent(
            model, tools=[duckduckgo_search_tool()], system_prompt=self._system_prompt
        )

    async def generate_response(self, context: str) -> str:
        """Generate AI response for given context."""
        result = await self.agent.run(context)
        return result.output

    def prepare_message_context(self, message: Message) -> str:
        lines = [
            f"Current user: {self._get_detailed_user_info(message.from_user)}",
            f"Current chat: {self._get_detailed_chat_info(message.chat)}",
        ]

        if message.reply_to_message:
            reply_to = message.reply_to_message
            reply_to_text = (
                reply_to.text or reply_to.caption or "(media message without text)"
            )
            lines.append(
                f"Replied to user: {self._get_detailed_user_info(reply_to.from_user)}"
            )
            lines.append(f'Replied to message: ```"{reply_to_text}"```')

        message_text = message.text or message.caption or "(media message without text)"
        lines.append(f"Message: ```{message_text}```")
        lines.append(f"Date: {message.date.isoformat()}")

        return "\n".join(lines)

    def _get_detailed_user_info(self, user: User) -> str:
        """Get comprehensive user information."""

        info_parts = [
            f"Name: {user.full_name}",
            f"ID: {user.id}",
            f"Bot: {user.is_bot}",
            f"Link to mention: {user.mention_markdown()}",
            f"@{user.username}" if user.username else "",
            f"Language: {user.language_code}" if user.language_code else "",
        ]
        return " | ".join(filter(None, info_parts))

    def _get_detailed_chat_info(self, chat: Chat) -> str:
        """Get comprehensive chat information."""

        info_parts = [
            f"Type: {chat.type}",
            f"Title: {chat.full_name}",
            f"ID: {chat.id}",
            f"Username: @{chat.username}" if chat.username else "",
        ]

        return " | ".join(filter(None, info_parts))


# Create a shared AI service instance
ai_service = AIService()


@router.message(or_f(DerpMentionFilter(), Command("derp"), F.chat.type == "private"))
class DerpResponseHandler(MessageHandler):
    """Class-based message handler for AI responses when 'derp' is mentioned, /derp command is used, or in private chats."""

    async def handle(self) -> Any:
        """Handle messages that mention 'derp', use /derp command, or are in private chats."""
        try:
            # Prepare comprehensive context using the AI service
            context = ai_service.prepare_message_context(self.event)

            # Generate AI response
            response_text = await ai_service.generate_response(context)

            # Return the response using SendMessage
            return await self.event.reply(response_text, parse_mode="Markdown")

        except ModelRetry as e:
            logger.error(f"Model retry error: {e}")
            return await self.event.reply(
                _("🤔 I'm having trouble thinking right now. Try again in a moment.")
            )
        except Exception as e:
            logger.error(f"Error in AI response handler: {e}")
            return await self.event.reply(
                _("😅 Something went wrong. I couldn't process that message.")
            )
