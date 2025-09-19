"""AI-powered response handler for messages mentioning 'derp' or 'дерп' and /derp command."""

from functools import cached_property
from typing import Any, List

import aiogram.exceptions
import logfire
from aiogram import F, Router, flags, md
from aiogram.filters import Command
from aiogram.handlers import MessageHandler
from aiogram.types import Message, ReactionTypeEmoji, Update
from aiogram.utils.i18n import gettext as _
from pydantic_ai import Agent, BinaryContent, ModelRetry
from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from ..common.database import get_database_client
from ..common.tg import Extractor
from ..config import settings
from ..filters import DerpMentionFilter
from ..queries.chat_settings_async_edgeql import ChatSettingsResult
from ..queries.select_active_updates_async_edgeql import select_active_updates
from ..tools.chat_memory import update_chat_memory_tool
from ..tools.deps import AgentDeps

router = Router(name="ai")


@logfire.instrument()
async def extract_media_for_ai(message: Message) -> List[BinaryContent]:
    """
    Extract supported media (photo, video, audio) from message for AI processing.

    Returns:
        List of BinaryContent objects ready for PydanticAI agent
    """
    media_content = []

    # Extract photo (includes image documents and static stickers)
    photo = Extractor.photo(message)
    if photo:
        try:
            image_data = await photo.download()
            media_content.append(
                BinaryContent(
                    data=image_data,
                    media_type=photo.media_type,
                )
            )
            logfire.info(f"Extracted photo from message {photo.message.message_id}")
        except Exception as e:
            logfire.warning(f"Failed to download photo: {e}")

    # Extract video (includes video stickers, animations, video notes)
    video = Extractor.video(message)
    if video:
        try:
            video_data = await video.download()
            media_content.append(
                BinaryContent(
                    data=video_data,
                    media_type=video.media_type,
                )
            )
            logfire.info(
                f"Extracted video from message {video.message.message_id}, duration: {video.duration}s"
            )
        except Exception as e:
            logfire.warning(f"Failed to download video: {e}")

    # Extract audio (includes audio files and voice messages)
    audio = Extractor.audio(message)
    if audio:
        try:
            audio_data = await audio.download()
            media_content.append(
                BinaryContent(
                    data=audio_data,
                    media_type=audio.media_type,
                )
            )
            logfire.info(
                f"Extracted audio from message {audio.message.message_id}, duration: {audio.duration}s"
            )
        except Exception as e:
            logfire.warning(f"Failed to download audio: {e}")

    # Extract document (includes PDF, Word, Excel, etc.)
    document = Extractor.document(message)
    if document and document.media_type == "application/pdf":
        try:
            document_data = await document.download()
            media_content.append(
                BinaryContent(
                    data=document_data,
                    media_type=document.media_type,
                )
            )
            logfire.info(
                f"Extracted document from message {document.message.message_id}"
            )
        except Exception as e:
            logfire.warning(f"Failed to download document: {e}")

    return media_content


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
        "(only bold, italic, underline, strikethrough, code, link). Responses should be under 200 words unless "
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
        "MEMORY MANAGEMENT: "
        "Use the update_chat_memory tool to remember important facts about users, their preferences, "
        "ongoing topics, or anything that would help personalize future conversations. "
    )

    @cached_property
    def agent(self) -> Agent:
        """Get the PydanticAI agent instance."""
        model_name = settings.default_llm_model.lower()

        # Google Models (default)
        if model_name in [
            "gemini-2.5-flash-preview-05-20",
            "gemini-2.5-pro-preview-05-06",
        ]:
            provider = GoogleProvider(api_key=settings.google_api_key)
            model_settings = GoogleModelSettings(
                google_thinking_config={"thinking_budget": 0},
            )
            model = GoogleModel(model_name, provider=provider)
            return Agent(
                model,
                tools=[
                    duckduckgo_search_tool(),
                    update_chat_memory_tool(),
                    # lambda: {"url_context": {}},
                    # lambda: {"google_search": {}},
                ],
                system_prompt=self._system_prompt,
                model_settings=model_settings,
                deps_type=AgentDeps,
            )

        # OpenAI Models
        elif model_name in [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "o3-mini",
            "o4-mini",
        ]:
            provider = OpenAIProvider(api_key=settings.openai_api_key)
            model = OpenAIModel(model_name, provider=provider)
            return Agent(
                model,
                tools=[duckduckgo_search_tool(), update_chat_memory_tool()],
                system_prompt=self._system_prompt,
                deps_type=AgentDeps,
            )

        # OpenRouter Models
        elif (
            "/" in model_name
        ):  # OpenRouter models typically have format "provider/model"
            provider = OpenRouterProvider(api_key=settings.openrouter_api_key)
            model = OpenAIModel(model_name, provider=provider)
            return Agent(
                model,
                tools=[duckduckgo_search_tool(), update_chat_memory_tool()],
                system_prompt=self._system_prompt,
                deps_type=AgentDeps,
            )

        raise ValueError(f"Unknown model: {model_name}")

    async def _generate_context(self, message: Message) -> str:
        """Generate context for the AI prompt from the message and recent chat history."""
        chat_settings: ChatSettingsResult = self.data.get("chat_settings")

        context_parts = []

        if chat_settings.llm_memory:
            context_parts.append("--- Chat Memory ---")
            context_parts.append(chat_settings.llm_memory)

        # Add recent chat history
        db_client = get_database_client()
        async with db_client.get_executor() as executor:
            recent_updates = await select_active_updates(
                executor, chat_id=message.chat.id, limit=15
            )

        if recent_updates:
            context_parts.append("--- Recent Chat History ---")
            for update in recent_updates:
                context_parts.append(f"Update: {update.raw_data}")

        # Add current message
        context_parts.append("--- Current Message ---")
        context_parts.append(
            message.model_dump_json(
                exclude_defaults=True, exclude_none=True, exclude_unset=True
            )
        )

        return "\n".join(context_parts)

    async def handle(self) -> Any:
        """Handle messages that mention 'derp', use /derp command, or are in private chats."""
        message: Message | None = None
        try:
            # Build context for the AI agent
            context = [await self._generate_context(self.event)]

            # Extract and add media content
            media_content = await extract_media_for_ai(self.event)
            context.extend(media_content)

            # Create dependencies for the agent
            deps = AgentDeps(message=self.event)

            # Generate AI response
            result = await self.agent.run(context, deps=deps)
            response_text = result.output

            if response_text == "":
                return await self.event.react(reaction=[ReactionTypeEmoji(emoji="👌")])

            try:
                message = await self.event.reply(response_text, parse_mode="Markdown")
            except aiogram.exceptions.TelegramBadRequest as exc:
                if "can't parse entities" in exc.message:
                    message = await self.event.reply(
                        md.quote(response_text), parse_mode="Markdown"
                    )

            # Wait for the middleware's database task to complete first to avoid race conditions
            if "db_task" in self.data:
                await self.data["db_task"]

            # Add my message to the database using the new method
            try:
                db_client = get_database_client()
                update = Update.model_validate(
                    {
                        "update_id": 0,
                        "message": message,
                    }
                )
                await db_client.create_bot_update_with_upserts(
                    update_id=0,
                    update_type="message",
                    raw_data=update.model_dump(
                        exclude_none=True, exclude_defaults=True
                    ),
                    user=message.from_user,
                    chat=message.chat,
                    sender_chat=None,
                )
                logfire.info(
                    f"Logged bot message {message.message_id} to database successfully"
                )
            except Exception:
                logfire.exception("Failed to log bot message to database")

            return message

        except ModelRetry:
            return await self.event.reply(
                _("🤔 I'm having trouble thinking right now. Try again in a moment.")
            )
        except Exception:
            if message is None:
                logfire.exception("Error in AI response handler")
                return await self.event.reply(
                    _("😅 Something went wrong. I couldn't process that message.")
                )
            else:
                logfire.exception("Error after sending AI response")
                return message
