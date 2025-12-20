"""AI-powered chat response handler using Pydantic-AI.

This handler processes chat messages and generates AI responses using
the provider-agnostic Pydantic-AI infrastructure with tools like
DuckDuckGo search and chat memory.

The handler is credit-aware:
- Free tier (no credits): Uses CHEAP model with 10 message context
- Paid tier (has credits): Uses STANDARD model with 100 message context
"""

from __future__ import annotations

import json
from typing import Any

import logfire
from aiogram import Bot, F, Router, flags
from aiogram.filters import Command
from aiogram.handlers import MessageHandler
from aiogram.types import Message, ReactionTypeEmoji
from aiogram.utils.i18n import gettext as _
from pydantic_ai import BinaryContent, UsageLimits
from pydantic_ai.exceptions import (
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)

from derp.common.extractor import Extractor
from derp.config import settings
from derp.credits import CONTEXT_LIMITS, CreditService
from derp.credits import ModelTier as CreditModelTier
from derp.db import DatabaseManager, get_db_manager, get_recent_messages
from derp.filters import DerpMentionFilter
from derp.llm import (
    AgentDeps,
    AgentResult,
    create_chat_agent,
)
from derp.llm import (
    ModelTier as LLMModelTier,
)
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.tools import create_chat_toolset

router = Router(name="chat")


@logfire.instrument("extract_media")
async def extract_media_for_agent(message: Message) -> list[BinaryContent]:
    """Extract supported media from message for agent processing.

    Converts Telegram media to Pydantic-AI BinaryContent format.
    """
    media_parts: list[BinaryContent] = []

    # Extract photo (includes image documents and static stickers)
    if photo := await Extractor.photo(message):
        try:
            image_data = await photo.download()
            media_parts.append(
                BinaryContent(
                    data=image_data,
                    media_type=photo.media_type or "image/jpeg",
                )
            )
            logfire.debug(
                "photo_extracted",
                media_type=photo.media_type,
                size=len(image_data),
            )
        except Exception:
            logfire.exception("photo_download_failed")

    # Extract video (includes video stickers, animations, video notes)
    if video := await Extractor.video(message):
        try:
            video_data = await video.download()
            media_parts.append(
                BinaryContent(
                    data=video_data,
                    media_type=video.media_type or "video/mp4",
                )
            )
            logfire.debug(
                "video_extracted",
                media_type=video.media_type,
                size=len(video_data),
            )
        except Exception:
            logfire.exception("video_download_failed")

    # Extract audio (includes audio files and voice messages)
    if audio := await Extractor.audio(message):
        try:
            audio_data = await audio.download()
            media_parts.append(
                BinaryContent(
                    data=audio_data,
                    media_type=audio.media_type or "audio/ogg",
                )
            )
            logfire.debug(
                "audio_extracted",
                media_type=audio.media_type,
                size=len(audio_data),
            )
        except Exception:
            logfire.exception("audio_download_failed")

    # Extract document (PDF only for now)
    if (
        document := await Extractor.document(message)
    ) and document.media_type == "application/pdf":
        try:
            document_data = await document.download()
            media_parts.append(
                BinaryContent(
                    data=document_data,
                    media_type=document.media_type,
                )
            )
            logfire.debug(
                "document_extracted",
                media_type=document.media_type,
                size=len(document_data),
            )
        except Exception:
            logfire.exception("document_download_failed")

    return media_parts


@logfire.instrument("build_context")
async def build_context_prompt(
    message: Message,
    db: DatabaseManager,
    context_limit: int = 100,
) -> str:
    """Build the context prompt for the agent.

    Includes chat info, recent history, and current message.
    Note: Chat memory is injected via the agent's system prompt.

    Args:
        message: The Telegram message.
        db: Database manager.
        context_limit: Max number of recent messages to include.
    """
    context_parts: list[str] = []

    # Chat info
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

    # Recent chat history from messages table (limited by tier)
    async with db.read_session() as session:
        recent_msgs = await get_recent_messages(
            session, chat_telegram_id=message.chat.id, limit=context_limit
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

    logfire.debug(
        "context_built",
        chars=len("\n".join(context_parts)),
        messages=len(recent_msgs) if recent_msgs else 0,
        limit=context_limit,
    )

    return "\n".join(context_parts)


@router.message(Command("context"), F.from_user.id.in_(settings.admin_ids))
async def show_context(message: Message, chat_model: ChatModel | None) -> None:
    """Admin command to show the context that would be sent to the agent."""
    db = get_db_manager()
    ctx = await build_context_prompt(message, db)
    stats = _("Context: {chars} chars, {msgs} messages").format(
        chars=len(ctx),
        msgs=ctx.count('"message_id"'),
    )
    await message.reply(stats)


@router.message(DerpMentionFilter())
@router.message(Command("derp"))
@router.message(F.chat.type == "private")
@router.message(F.reply_to_message.from_user.id == settings.bot_id)
class ChatAgentHandler(MessageHandler):
    """Message handler for AI responses using Pydantic-AI agents.

    Credit-aware handler that selects model tier and context limit
    based on user/chat credit balance:
    - No credits: CHEAP model, 10 message context
    - Has credits: STANDARD model, 100 message context
    """

    @flags.chat_action
    async def handle(self) -> Any:
        """Handle messages using the Pydantic-AI chat agent."""
        # Extract dependencies from middleware data
        db: DatabaseManager = self.data.get("db") or get_db_manager()
        bot: Bot = self.data.get("bot") or self.event.bot
        user_model: UserModel | None = self.data.get("user_model")
        chat_model: ChatModel | None = self.data.get("chat_model")
        credit_service: CreditService | None = self.data.get("credit_service")

        # Determine tier and context limit based on credits
        tier = LLMModelTier.CHEAP  # Default for free tier
        context_limit = CONTEXT_LIMITS[CreditModelTier.CHEAP]

        tier_map = {
            CreditModelTier.CHEAP: LLMModelTier.CHEAP,
            CreditModelTier.STANDARD: LLMModelTier.STANDARD,
            CreditModelTier.PREMIUM: LLMModelTier.PREMIUM,
        }

        if user_model and chat_model and credit_service:
            (
                credit_tier,
                _model_id,
                context_limit,
            ) = await credit_service.get_orchestrator_config(user_model, chat_model)
            tier = tier_map.get(credit_tier, LLMModelTier.CHEAP)

        # Create agent dependencies with determined tier
        deps = AgentDeps(
            message=self.event,
            db=db,
            bot=bot,
            user_model=user_model,
            chat_model=chat_model,
            tier=tier,
        )

        try:
            with logfire.span(
                "chat_agent_run",
                _tags=["agent", "chat"],
                telegram_chat_id=self.event.chat.id,
                telegram_user_id=self.event.from_user and self.event.from_user.id,
                telegram_message_id=self.event.message_id,
                model_tier=deps.tier.value,
                context_limit=context_limit,
            ) as span:
                # Build context prompt with tier-appropriate limit
                context = await build_context_prompt(self.event, db, context_limit)
                span.set_attribute("derp.context_chars", len(context))
                span.set_attribute(
                    "derp.context_messages", context.count('"message_id"')
                )

                # Extract media
                media_parts = await extract_media_for_agent(self.event)
                span.set_attribute("derp.has_media", len(media_parts) > 0)
                span.set_attribute("derp.media_count", len(media_parts))

                # Build the user prompt with context and media
                user_prompt: list[str | BinaryContent] = [context]
                user_prompt.extend(media_parts)

                # Create and run the agent with tools
                agent = create_chat_agent(deps.tier)
                toolset = create_chat_toolset()

                logfire.info(
                    "running_agent",
                    tier=deps.tier.value,
                    context_limit=context_limit,
                    tools=len(toolset._tools) if hasattr(toolset, "_tools") else 0,
                )

                result = await agent.run(
                    user_prompt,
                    deps=deps,
                    toolsets=[toolset],
                    usage_limits=UsageLimits(tool_calls_limit=3),
                )

                # Convert to AgentResult and send response
                agent_result = AgentResult.from_run_result(result)

                span.set_attribute("derp.response_has_text", bool(agent_result.text))
                span.set_attribute("derp.response_images", len(agent_result.images))

                # Handle empty response
                if not agent_result.has_content:
                    try:
                        await self.event.react(reaction=[ReactionTypeEmoji(emoji="👌")])
                        logfire.debug("empty_response_reacted")
                    except Exception:
                        logfire.debug("empty_response_react_failed")
                    return None

                return await agent_result.reply_to(self.event)

        except ModelHTTPError as exc:
            if exc.status_code == 429:
                logfire.warning(
                    "chat_rate_limited",
                    status_code=exc.status_code,
                    model=exc.model_name,
                )
                return await self.event.reply(
                    _(
                        "⏳ The AI service is overloaded right now.\n\n"
                        "This happens during peak usage. Please wait 30-60 seconds "
                        "and try again."
                    )
                )
            logfire.exception("chat_model_http_error", status_code=exc.status_code)
            return await self.event.reply(
                _("😅 Something went wrong. I couldn't process that message.")
            )
        except UsageLimitExceeded:
            logfire.warning("agent_usage_limit_exceeded", _exc_info=True)
            return await self.event.reply(
                _("⚠️ Too many tool calls. Please try a simpler request.")
            )
        except UnexpectedModelBehavior:
            logfire.warning("agent_unexpected_behavior", _exc_info=True)
            return await self.event.reply(
                _(
                    "⏳ I'm getting too many requests right now. "
                    "Please try again in about 30 seconds."
                )
            )
        except Exception:
            logfire.exception("chat_agent_failed")
            return await self.event.reply(
                _("😅 Something went wrong. I couldn't process that message.")
            )
