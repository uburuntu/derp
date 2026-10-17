"""Free models: the last rung after paid and sponsored, only with consent.

Free OpenRouter models are not zero-data-retention: their providers may store
prompts. So a person opts in once for their private chats (recorded with the
policy text version they saw), and in groups an admin turns them on for the
whole chat, which posts a short public notice. After consenting, the question
that hit the wall is answered right away.
"""

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from derp.billing.command import group_chooser, personal_menu
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.locales import resolve, t
from derp.memory.notes import is_admin
from derp.telegram.sender import Target, send_markdown

if TYPE_CHECKING:  # the runtime imports this module
    from derp.agent.runtime import Runtime

POLICY_VERSION = "2026-10"  # bump when the free-model notice changes: consent is asked again


class FreeModels(CallbackData, prefix="free"):
    action: str  # consent (a person, private chats) | enable (an admin, this group) | buy


def consented(user: User | None) -> bool:
    return user is not None and user.free_models_consent_version == POLICY_VERSION


def offer(locale: str, *, group: bool) -> tuple[str, InlineKeyboardMarkup]:
    """The out-of-Derpcoins notice with buttons to turn free models on or buy Derpcoins."""
    action, text, button = (
        ("enable", "money.free_offer_group", "money.free_enable")
        if group
        else ("consent", "money.free_offer", "money.free_consent")
    )
    free_button = InlineKeyboardButton(text=t(locale, button), callback_data=FreeModels(action=action).pack())
    buy_button = InlineKeyboardButton(
        text=t(locale, "start.buy"), callback_data=FreeModels(action="buy").pack()
    )
    return t(locale, text), InlineKeyboardMarkup(inline_keyboard=[[free_button], [buy_button]])


async def on_free(
    query: CallbackQuery,
    callback_data: FreeModels,
    db: Database,
    settings: Settings,
    bot: Bot,
    runtime: Runtime,
) -> None:
    locale = resolve(query.from_user.language_code)
    message = query.message
    if not isinstance(message, Message):
        await query.answer()
        return
    if callback_data.action == "buy":
        await query.answer()
        thread = message.message_thread_id if message.is_topic_message else None
        target = Target(message.chat.id, thread)
        if message.chat.type == "private":
            text, markup = await personal_menu(db, settings, query.from_user.id, locale)
            await send_markdown(bot, target, text, reply_markup=markup)
        else:
            await send_markdown(bot, target, t(locale, "billing.who"), reply_markup=group_chooser(locale))
        return
    if callback_data.action == "consent":
        async with db.transaction() as session:
            user = await session.get(User, query.from_user.id)
            if user is None:
                await query.answer()
                return
            user.free_models_consent_at = datetime.now(UTC)
            user.free_models_consent_version = POLICY_VERSION
        await query.answer(t(locale, "money.free_on"))
    else:
        async with db.session() as session:
            chat = await session.get(Chat, message.chat.id)
        if chat is None or not await is_admin(bot, chat, query.from_user.id):
            await query.answer(t(locale, "money.free_admins_only"), show_alert=True)
            return
        async with db.transaction() as session:
            row = await session.get(Chat, chat.id)
            if row is None:
                await query.answer()
                return
            row.free_models = True
            chat = row
        await query.answer()
        await send_markdown(bot, Target(chat.id), t(chat.locale or locale, "money.free_enabled"))
    with contextlib.suppress(TelegramAPIError):
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id, message_id=message.message_id, reply_markup=None
        )
    trigger = message.reply_to_message  # the question that ran out of Derpcoins
    if trigger is not None and trigger.from_user is not None:
        async with db.session() as session:
            chat_row = await session.get(Chat, message.chat.id)
        if chat_row is not None:
            await runtime.answer(trigger, chat_row)


def build_router() -> Router:
    router = Router(name="free-models")
    router.callback_query.register(on_free, FreeModels.filter(F.action.in_({"consent", "enable", "buy"})))
    return router
