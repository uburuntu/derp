"""Inline query handler using Pydantic-AI.

This handler processes inline queries for quick AI responses,
using the CHEAP tier for cost efficiency on high-volume queries.
"""

from __future__ import annotations

import uuid
from typing import Any

import logfire
from aiogram import Bot, F, Router, html
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
)
from aiogram.utils.i18n import gettext as _
from pydantic_ai.exceptions import UnexpectedModelBehavior

from derp.config import settings
from derp.llm import ModelTier, create_inline_agent

router = Router(name="inline")


@router.inline_query(F.query == "")
async def inline_query_empty(query: InlineQuery) -> Any:
    """Handle empty inline queries."""
    result_id = str(uuid.uuid4())
    result = InlineQueryResultArticle(
        id=result_id,
        title=_("🤖 Ask Derp"),
        description=_("Start typing to get an AI-powered response."),
        input_message_content=InputTextMessageContent(
            message_text=html.italic(_("🤖 Please enter a prompt for Derp AI."))
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_("Add Derp to your chat"),
                        url=f"https://t.me/{settings.bot_username}?startgroup=true",
                    )
                ]
            ]
        ),
    )
    await query.answer([result], cache_time=300)


@router.inline_query(F.query != "")
async def inline_query_with_text(query: InlineQuery) -> Any:
    """Handle non-empty inline queries."""
    result_id = str(uuid.uuid4())
    user_input = query.query[:200] or "..."

    result = InlineQueryResultArticle(
        id=result_id,
        title=_("🤖 Ask Derp"),
        description=_("Get an AI-powered response for: {user_input}").format(
            user_input=user_input
        ),
        input_message_content=InputTextMessageContent(
            message_text=html.italic(
                _("🧠 Thinking about: {user_input}").format(user_input=user_input)
            )
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_("Add Derp to your chat"),
                        url=f"https://t.me/{settings.bot_username}?startgroup=true",
                    )
                ]
            ]
        ),
    )
    await query.answer(
        [result],
        button=InlineQueryResultsButton(
            text=_("Start personal chat"),
            start_parameter="start",
        ),
        cache_time=300,
    )


@router.chosen_inline_result()
async def chosen_inline_result(chosen_result: ChosenInlineResult, bot: Bot) -> None:
    """Handle chosen inline results - generate and update with AI response."""
    if not chosen_result.inline_message_id:
        return

    # Build prompt with user info
    user_info = chosen_result.from_user.model_dump_json(
        exclude_defaults=True, exclude_none=True, exclude_unset=True
    )
    prompt = f"User: {user_info}\nQuery: {chosen_result.query}"

    try:
        with logfire.span(
            "inline_agent_run",
            _tags=["agent", "inline"],
            telegram_user_id=chosen_result.from_user.id,
            query_length=len(chosen_result.query),
        ):
            # Use CHEAP tier for inline queries (high volume, low cost)
            agent = create_inline_agent(ModelTier.CHEAP)
            result = await agent.run(prompt)

            if result.output:
                response_text = (
                    f"Prompt: {chosen_result.query}\n\nResponse:\n{result.output}"[
                        :4096
                    ]
                )
                await bot.edit_message_text(
                    response_text,
                    inline_message_id=chosen_result.inline_message_id,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=_("Add Derp to your chat"),
                                    url=f"https://t.me/{settings.bot_username}?startgroup=true",
                                )
                            ]
                        ]
                    ),
                )
                logfire.info("inline_response_sent", length=len(result.output))
            else:
                await bot.edit_message_text(
                    _(
                        "🤯 My circuits are a bit tangled. "
                        "I couldn't generate a response."
                    ),
                    inline_message_id=chosen_result.inline_message_id,
                )
                logfire.warning("inline_empty_response")

    except UnexpectedModelBehavior:
        logfire.warning("inline_rate_limited")
        await bot.edit_message_text(
            _(
                "⏳ I'm getting too many requests right now. "
                "Please try again in about 30 seconds."
            ),
            inline_message_id=chosen_result.inline_message_id,
        )
    except Exception:
        logfire.exception("inline_handler_failed")
        await bot.edit_message_text(
            _("😅 Something went wrong. I couldn't process that."),
            inline_message_id=chosen_result.inline_message_id,
        )
