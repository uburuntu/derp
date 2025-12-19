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
from pydantic_ai.exceptions import ModelHTTPError

from derp.common.sender import MessageSender
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
    sender: MessageSender,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
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
        return await sender.reply(
            _(
                "🧠 **Deep Thinking Mode**\n\n"
                "Use Gemini 3 Pro for complex math, logic puzzles, "
                "or problems that need careful analysis.\n\n"
                "Usage: /think <your problem or question>"
            ),
        )

    if not user_model or not chat_model:
        return await message.reply(
            _("😅 Could not verify your access. Please try again.")
        )

    result = await credit_service.check_tool_access(
        user_model, chat_model, "think_deep"
    )

    if not result.allowed:
        return await sender.reply(
            _(
                "🧠 Deep thinking requires credits.\n\n"
                "✨ {reason}\n\n"
                "💡 Use /buy to get credits!"
            ).format(reason=result.reject_reason),
        )

    logfire.info(
        "think_command_started",
        user_id=user_model.telegram_id,
        prompt_length=len(prompt),
    )

    try:
        agent = create_chat_agent(ModelTier.PREMIUM)

        deps = AgentDeps(
            message=message,
            db=get_db_manager(),
            bot=message.bot,
            user_model=user_model,
            chat_model=chat_model,
            tier=ModelTier.PREMIUM,
        )

        thinking_prompt = (
            "You are in deep thinking mode. Take your time to carefully analyze "
            "the problem step by step. Show your reasoning process clearly.\n\n"
            f"**Problem:**\n{prompt}"
        )

        agent_result = await agent.run(thinking_prompt, deps=deps)

        idempotency_key = f"think:{chat_model.telegram_id}:{message.message_id}"
        await credit_service.deduct(
            result,
            user_model,
            chat_model,
            "think_deep",
            idempotency_key=idempotency_key,
        )

        logfire.info(
            "think_command_completed",
            user_id=user_model.telegram_id,
            response_length=len(agent_result.output),
        )

        return await sender.reply(
            f"🧠 **Deep Thinking Result:**\n\n{agent_result.output}",
        )

    except ModelHTTPError as exc:
        if exc.status_code == 429:
            logfire.warning(
                "think_rate_limited",
                status_code=exc.status_code,
                model=exc.model_name,
                user_id=user_model.telegram_id if user_model else None,
            )
            return await message.reply(
                _(
                    "⏳ The AI service is overloaded right now.\n\n"
                    "This happens during peak usage. Please wait 30-60 seconds "
                    "and try again."
                )
            )
        logfire.exception(
            "think_model_http_error",
            status_code=exc.status_code,
            user_id=user_model.telegram_id if user_model else None,
        )
        return await message.reply(
            _("😅 Something went wrong during deep thinking. Please try again.")
        )
    except Exception:
        logfire.exception(
            "think_command_failed",
            user_id=user_model.telegram_id if user_model else None,
        )
        return await message.reply(
            _("😅 Something went wrong during deep thinking. Please try again.")
        )
