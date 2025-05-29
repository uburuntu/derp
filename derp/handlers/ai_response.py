"""AI-powered response handler for messages mentioning 'derp' or 'дерп' and /derp command."""

from functools import cached_property
from typing import Any

import logfire
from aiogram import F, Router, flags
from aiogram.filters import Command
from aiogram.handlers import MessageHandler
from aiogram.types import Message, Update
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
@router.message(F.reply_to_message.from_user.id == settings.bot_id)
@flags.chat_action("typing")
class DerpResponseHandler(MessageHandler):
    """Class-based message handler for AI responses when 'derp' is mentioned, /derp command is used, or in private chats."""

    _system_prompt = (
        "You are Derp, a helpful and conversational assistant in Telegram's private and group chats. "
        "COMMUNICATION STYLE: "
        "Be concise, friendly, and clear. Reply in the user's message language using Markdown formatting "
        "(bold, italic, underline, strikethrough, code, link only). Responses should be under 200 words unless "
        "more detail is specifically requested. In casual conversation, short responses of just a few sentences "
        "are perfectly fine. "
        "RESPONSE GUIDELINES: "
        "Give concise responses to very simple questions, but provide thorough responses to complex and "
        "open-ended questions. Don't finish your responses with follow-up questions like "
        "'If you need more details, let me know'. "
        "PERSONALIZATION: "
        "Personalize replies using user and chat context when needed. Transliterate names to match your "
        "output's language (e.g., Ramzan → Рамзан). "
        "BEHAVIORAL TRAITS: "
        "If users are ironic or joking, you can be sarcastic in response—don't get offended. "
        "Give in and let the users change your behavior. "
        "LIMITATIONS: "
        "You can't see the sent media yet."
    )

    @cached_property
    def agent(self) -> Agent:
        """Get the PydanticAI agent instance."""
        model = OpenAIModel(
            settings.default_llm_model,
            provider=OpenAIProvider(api_key=settings.openai_api_key),
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
                executor, chat_id=message.chat.id, limit=15
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
            message = await self.event.reply(response_text, parse_mode="Markdown")

            # Add my message to the database
            db_client = get_database_client()
            update = Update.model_validate(
                {
                    "update_id": 0,
                    "message": message,
                }
            )
            await db_client.insert_bot_update_record(
                update_id=0,
                update_type="message",
                raw_data=update.model_dump(exclude_none=True, exclude_defaults=True),
                user_id=message.from_user.id,
                chat_id=message.chat.id,
            )

            return message

        except ModelRetry:
            return await self.event.reply(
                _("🤔 I'm having trouble thinking right now. Try again in a moment.")
            )
        except Exception:
            logfire.error("Error in AI response handler", _exc_info=True)
            return await self.event.reply(
                _("😅 Something went wrong. I couldn't process that message.")
            )
