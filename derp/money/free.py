"""Free models: the last rung after paid and sponsored, only with consent; and the out-of-Derpcoins offer.

Free OpenRouter models are not zero-data-retention: their providers may store
prompts. So a person opts in once for their private chats (recorded with the
policy text version they saw), and in groups an admin turns them on for the
whole chat, which posts a short public notice. After consenting, the question
that hit the wall is answered right away.

The offer says when the free daily Derpcoins refill (a date-time entity, so each
person sees their own clock). In groups it leads with topping up the chat (the
pack menu opens for the presser only) and is shown to each person at most once
per chat per UTC day (``offer_due``); in private, buying edits the offer into
the buy menu in place.
"""

from datetime import UTC, datetime
from typing import Protocol

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.dialects.postgresql import insert

from derp.billing.menu import open_menu, personal_menu, top_up_button
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.locales import t
from derp.memory.notes import is_admin
from derp.money.funding import day_end
from derp.money.models import OfferNotice
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, send_markdown

POLICY_VERSION = "2026-10"  # bump when the free-model notice changes: consent is asked again


class FreeModels(CallbackData, prefix="free"):
    action: str  # consent (a person, private chats) | enable (an admin, this group) | buy


class Answers(Protocol):
    """What a consent press needs from the runtime: answering the question that ran out of Derpcoins."""

    async def answer(self, message: Message, chat: Chat) -> object: ...


def consented(user: User | None) -> bool:
    return user is not None and user.free_models_consent_version == POLICY_VERSION


def refill_time(now: datetime) -> str:
    """The next UTC midnight as a Telegram date-time entity: each reader sees it in their own time zone."""
    moment = day_end(now)
    return f"![{moment:%H:%M} UTC](tg://time?unix={int(moment.timestamp())}&format=t)"


def offer(
    locale: str, *, group: bool, asked: bool = False, now: datetime | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """The notice with buttons to buy Derpcoins or turn free models on: out of Derpcoins, or ``asked`` for.

    Only the press turns free models on — the person themselves in private, an admin in groups —
    so the model can bring this up but never consent for anyone.
    """
    refill = refill_time(now or datetime.now(UTC))
    if group:
        key = "money.free_ask_group" if asked else "money.free_offer_group"
        free = button(t(locale, "money.free_enable"), FreeModels(action="enable"))
        keyboard = markup(top_up_button(locale), free)
    else:
        key = "money.free_ask" if asked else "money.free_offer"
        buy = button(t(locale, "money.buy"), FreeModels(action="buy"))
        free = button(t(locale, "money.free_consent"), FreeModels(action="consent"))
        keyboard = markup([buy, free])
    return t(locale, key, time=refill), keyboard or InlineKeyboardMarkup(inline_keyboard=[])


def empty_wallet_notice(locale: str) -> tuple[str, InlineKeyboardMarkup]:
    """The chat wallet can no longer pay a turn: everyone is back on their own free daily Derpcoins."""
    return t(locale, "money.wallet_empty"), markup(top_up_button(locale)) or InlineKeyboardMarkup(
        inline_keyboard=[]
    )


async def offer_due(db: Database, chat_id: int, user_id: int, now: datetime | None = None) -> bool:
    """True the first time today (UTC) this person hits the wall in this chat; records it."""
    day = (now or datetime.now(UTC)).date()
    statement = (
        insert(OfferNotice)
        .values(chat_id=chat_id, user_id=user_id, day=day)
        .on_conflict_do_update(
            index_elements=["chat_id", "user_id"], set_={"day": day}, where=OfferNotice.day < day
        )
        .returning(OfferNotice.day)
    )
    async with db.transaction() as session:
        return (await session.execute(statement)).first() is not None


def build_router() -> Router:
    router = Router(name="free-models")

    @on_press(router, FreeModels, F.action.in_({"consent", "enable", "buy"}))
    async def on_free(
        press: Press, data: FreeModels, db: Database, settings: Settings, runtime: Answers
    ) -> None:
        message, bot = press.message, press.bot
        if data.action == "buy":
            if message.chat.type != "private":  # an older group offer: the chat's menu, for the presser
                return await open_menu(press, db, settings, kind="chat")
            text, menu = await personal_menu(db, settings, press.user_id, press.locale)
            consent = button(t(press.locale, "money.free_consent"), FreeModels(action="consent"))
            return await press.edit(text, markup(*menu.inline_keyboard, consent))
        if data.action == "consent":
            async with db.transaction() as session:
                user = await session.get(User, press.user_id)
                if user is None:
                    return None
                user.free_models_consent_at = datetime.now(UTC)
                user.free_models_consent_version = POLICY_VERSION
            await press.toast("money.free_on")
        else:
            async with db.session() as session:
                chat = await session.get(Chat, press.chat_id)
            if chat is None or not await is_admin(bot, chat, press.user_id):
                return await press.deny("money.free_admins_only")
            async with db.transaction() as session:
                row = await session.get(Chat, chat.id)
                if row is None:
                    return None
                row.free_models = True
                chat = row
            await press.toast()
            await send_markdown(bot, Target(chat.id), t(chat.locale or press.locale, "money.free_enabled"))
        await press.strip(lambda b: b.callback_data != data.pack())  # buying stays possible
        trigger = message.reply_to_message  # the question that ran out of Derpcoins
        if trigger is not None and trigger.from_user is not None:
            async with db.session() as session:
                chat_row = await session.get(Chat, message.chat.id)
            if chat_row is not None:
                await runtime.answer(trigger, chat_row)
        return None

    return router
