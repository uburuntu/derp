"""Handler for deep thinking commands (/think).

Uses the PREMIUM model tier (Gemini 3 Pro) for complex reasoning tasks.
Shares credit checks and limits with the agent tool `think_deep`.
"""

from __future__ import annotations

import logfire
from aiogram import Router, flags
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.credits import CreditService
from derp.db import get_db_manager
from derp.llm import AgentDeps, create_chat_agent
from derp.llm.providers import ModelTier
from derp.models import Chat as ChatModel
from derp.models import User as UserModel

router = Router(name="think")


@router.message(Command("think"))
@flags.chat_action(initial_sleep=1, action="typing")
async def handle_think(
    message: Message,
    chat_settings: ChatModel | None = None,
    user: UserModel | None = None,
) -> Message:
    """Handle /think command for deep reasoning using Gemini 3 Pro.

    Uses the PREMIUM model tier (gemini-3-pro-preview) for complex
    reasoning tasks with extended thinking capabilities.

    Reference: https://ai.google.dev/gemini-api/docs/models#gemini-3
    """
    prompt = message.text
    if prompt:
        prompt = prompt.removeprefix("/think").strip()

    if not prompt:
        return await message.reply(
            _(
                "🧠 **Deep Thinking Mode**\n\n"
                "Use Gemini 3 Pro for complex math, logic puzzles, "
                "or problems that need careful analysis.\n\n"
                "Usage: /think <your problem or question>"
            ),
            parse_mode="Markdown",
        )

    # Check credits
    if not user or not chat_settings:
        return await message.reply(
            _("😅 Could not verify your access. Please try again.")
        )

    db = get_db_manager()
    async with db.session() as session:
        service = CreditService(session)
        result = await service.check_tool_access(
            user_id=user.id,
            chat_id=chat_settings.id,
            user_telegram_id=user.telegram_id,
            chat_telegram_id=chat_settings.telegram_id,
            tool_name="think_deep",
        )

        if not result.allowed:
            return await message.reply(
                _(
                    "🧠 Deep thinking requires credits.\n\n"
                    "✨ {reason}\n\n"
                    "💡 Use /buy to get credits!"
                ).format(reason=result.reject_reason),
                parse_mode="Markdown",
            )

        logfire.info(
            "think_command_started",
            user_id=user.telegram_id,
            prompt_length=len(prompt),
        )

        try:
            # Create PREMIUM agent (Gemini 3 Pro)
            agent = create_chat_agent(ModelTier.PREMIUM)

            # Build deps for the agent
            deps = AgentDeps(
                message=message,
                db=db,
                bot=message.bot,
                chat=chat_settings,
                user=user,
                tier=ModelTier.PREMIUM,
            )

            # Run with thinking-optimized prompt
            thinking_prompt = (
                "You are in deep thinking mode. Take your time to carefully analyze "
                "the problem step by step. Show your reasoning process clearly.\n\n"
                f"**Problem:**\n{prompt}"
            )

            agent_result = await agent.run(thinking_prompt, deps=deps)

            # Deduct credits after successful generation
            idempotency_key = f"think:{chat_settings.telegram_id}:{message.message_id}"
            await service.deduct(
                result,
                user_id=user.id,
                chat_id=chat_settings.id,
                tool_name="think_deep",
                idempotency_key=idempotency_key,
            )

            logfire.info(
                "think_command_completed",
                user_id=user.telegram_id,
                response_length=len(agent_result.output),
            )

            # Send response with markdown, fallback to plain text
            try:
                return await message.reply(
                    f"🧠 **Deep Thinking Result:**\n\n{agent_result.output}",
                    parse_mode="Markdown",
                )
            except Exception:
                return await message.reply(
                    f"🧠 Deep Thinking Result:\n\n{agent_result.output}",
                    parse_mode=None,
                )

        except Exception:
            logfire.exception("think_command_failed", user_id=user.telegram_id)
            return await message.reply(
                _("😅 Something went wrong during deep thinking. Please try again.")
            )

