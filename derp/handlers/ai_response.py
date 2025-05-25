"""AI-powered response handler for messages mentioning 'derp' or 'дерп' and /derp command."""

from functools import cached_property
from typing import Any

import logfire
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.handlers import MessageHandler
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from ..common.database import get_database_client
from ..config import settings
from ..filters import DerpMentionFilter
from ..queries.select_active_updates_async_edgeql import select_active_updates

router = Router(name="ai")


@router.message(DerpMentionFilter())
@router.message(Command("derp"))
@router.message(F.chat.type == "private")
class DerpResponseHandler(MessageHandler):
    """Class-based message handler for AI responses when 'derp' is mentioned, /derp command is used, or in private chats."""

    _system_prompt = (
        "You are a helpful, conversational assistant in Telegram, called Derp. "
        "Reply in the user's message language, using Markdown (bold, italic, underline, strikethrough, code, link only). "
        "Be concise, friendly, and clear—responses under 200 words unless more detail is requested. "
        "Personalize replies using user and chat context, when needed. "
        "Transliterate names to match your output's language (e.g., Ramzan → Рамзан). "
        "If users are ironic or joking, you can be a bit sarcastic in response; don't get offended. "
        "You can't see the sent media yet. "
        "Don't finish your response with follow up questions like `If you need more details, let me know`. "
    )

    @cached_property
    def agent(self) -> Agent:
        """Get the PydanticAI agent instance."""
        model = OpenAIModel(
            "o3-mini", provider=OpenAIProvider(api_key=settings.openai_api_key)
        )
        return Agent(
            model, tools=[duckduckgo_search_tool()], system_prompt=self._system_prompt
        )

    async def _generate_context(self, message: Message) -> str:
        """Generate context for the AI prompt from the message and recent chat history."""
        context_parts = []

        # Add recent chat history
        db_client = get_database_client()
        async with db_client.get_executor() as executor:
            recent_updates = await select_active_updates(
                executor, chat_id=message.chat.id, limit=10
            )

        if recent_updates:
            context_parts.append("--- Recent Chat History ---")
            for update in reversed(recent_updates):  # Show oldest first
                context_parts.append(f"Update: {update.raw_data}")

        # Add current message
        context_parts.append("--- Current Message ---")
        context_parts.append(message.model_dump_json(exclude_none=True))

        return "\n".join(context_parts)

    async def handle(self) -> Any:
        """Handle messages that mention 'derp', use /derp command, or are in private chats."""
        try:
            # Generate context around the incoming message
            context = await self._generate_context(self.event)

            # Generate AI response
            result = await self.agent.run(context)
            response_text = result.output

            # Return the response using SendMessage
            return await self.event.reply(response_text, parse_mode="Markdown")

        except ModelRetry:
            return await self.event.reply(
                _("🤔 I'm having trouble thinking right now. Try again in a moment.")
            )
        except Exception:
            logfire.error("Error in AI response handler", _exc_info=True)
            return await self.event.reply(
                _("😅 Something went wrong. I couldn't process that message.")
            )
