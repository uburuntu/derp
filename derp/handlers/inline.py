"""Telegram adapters for bounded free inline answers.

Provider admission and execution live in the inline feature service.
"""

from __future__ import annotations

import uuid
from typing import Any

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

from derp.common.sender import MessageSender
from derp.config import settings
from derp.features.inline_chat import (
    InlineChatCompleted,
    InlineChatExhausted,
    InlineChatFailed,
    InlineChatFailureReason,
    InlineChatFeatureService,
    InlineChatInvalid,
)
from derp.models import User as UserModel
from derp.observability import report_exception

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
async def chosen_inline_result(
    chosen_result: ChosenInlineResult,
    bot: Bot,
    inline_chat_service: InlineChatFeatureService,
    user_model: UserModel | None = None,
) -> None:
    """Translate one typed inline outcome into an edit of the chosen result."""
    if not chosen_result.inline_message_id:
        return

    sender = MessageSender(bot=bot, chat_id=0)  # chat_id unused for inline
    if user_model is None:
        await sender.edit_inline(
            chosen_result.inline_message_id,
            _("I couldn't verify your inline allowance. Open Derp and try again."),
            reply_markup=_start_personal_chat_markup(),
        )
        return
    try:
        outcome = await inline_chat_service.answer(
            user_id=user_model.id,
            query=chosen_result.query,
        )
    except Exception as exc:
        report_exception(
            "inline_handler_failed",
            exception=exc,
            telegram_user_id=chosen_result.from_user.id,
        )
        await sender.edit_inline(
            chosen_result.inline_message_id,
            _("I couldn't answer this inline request right now. Try again later."),
            reply_markup=_retry_inline_markup(),
        )
        return

    if isinstance(outcome, InlineChatCompleted):
        await sender.edit_inline(
            chosen_result.inline_message_id,
            outcome.text,
            reply_markup=_add_to_chat_markup(),
        )
        return
    if isinstance(outcome, InlineChatExhausted):
        await sender.edit_inline(
            chosen_result.inline_message_id,
            _("Daily inline limit reached. Try again after 00:00 UTC."),
            reply_markup=_start_personal_chat_markup(),
        )
        return
    if isinstance(outcome, InlineChatInvalid):
        await sender.edit_inline(
            chosen_result.inline_message_id,
            _("This inline request is empty or too long. Shorten it and try again."),
            reply_markup=_retry_inline_markup(),
        )
        return
    if isinstance(outcome, InlineChatFailed):
        await sender.edit_inline(
            chosen_result.inline_message_id,
            _inline_failure_text(outcome.reason),
            reply_markup=(
                _start_personal_chat_markup()
                if outcome.reason is InlineChatFailureReason.ALLOWANCE_UNAVAILABLE
                else _retry_inline_markup()
            ),
        )
        return
    raise TypeError(f"unsupported inline outcome: {type(outcome).__name__}")


def _inline_failure_text(reason: InlineChatFailureReason) -> str:
    if reason is InlineChatFailureReason.ALLOWANCE_UNAVAILABLE:
        return _("I couldn't verify your inline allowance. Open Derp and try again.")
    if reason is InlineChatFailureReason.PROVIDER_TIMEOUT:
        return _("The model took too long to answer. Try again later.")
    if reason is InlineChatFailureReason.PROVIDER_REJECTED:
        return _("The model couldn't answer this request. Try a different question.")
    if reason is InlineChatFailureReason.UNUSABLE_OUTPUT:
        return _("The model returned no usable answer. Try a different question.")
    return _("I couldn't answer this inline request right now. Try again later.")


def _add_to_chat_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Add Derp to your chat"),
                    url=f"https://t.me/{settings.bot_username}?startgroup=true",
                )
            ]
        ]
    )


def _start_personal_chat_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Start personal chat"),
                    url=f"https://t.me/{settings.bot_username}?start=inline",
                )
            ]
        ]
    )


def _retry_inline_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Ask another question"),
                    switch_inline_query_current_chat="",
                )
            ]
        ]
    )
