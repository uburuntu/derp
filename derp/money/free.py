"""Free models: the last rung after paid and sponsored, only with consent; and the money notices of a run.

Free OpenRouter models are not zero-data-retention: their providers may store
prompts. So a person opts in once for their private chats (recorded with the
policy text version they saw), and in groups an admin turns them on for the
whole chat, which posts a short public notice. After consenting, the question
that hit the wall is answered right away.

The out-of-Derpcoins offer says when the free daily Derpcoins refill (a date-time
entity, so each person sees their own clock) and leads with buying (``buy_button``).
In groups it is shown to each person at most once per chat per UTC day
(``chat_members.offered_on``); later unfunded asks get a 🙈 reaction. After a charge
leaves a chat wallet unable to pay a turn, one silent line says so, once per depletion.
"""

import contextlib
import logging
from datetime import UTC, datetime
from typing import Protocol

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message, ReactionTypeEmoji
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert

from derp.billing.menu import buy_button, top_up_button
from derp.db.engine import Database
from derp.history.models import Chat, ChatMember, User
from derp.locales import t
from derp.memory.notes import is_admin
from derp.money.funding import Funder, Funds, day_end
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, send_markdown

POLICY_VERSION = "2026-10"  # bump when the free-model notice changes: consent is asked again
log = logging.getLogger("derp.money")


class FreeModels(CallbackData, prefix="free"):
    action: str  # consent (a person, private chats) | enable (an admin, this group)


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
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The notice with buttons to buy Derpcoins or turn free models on: out of Derpcoins, or ``asked`` for.

    Only the press turns free models on — the person themselves in private, an admin in groups —
    so the model can bring this up but never consent for anyone.
    """
    refill = refill_time(now or datetime.now(UTC))
    if group:
        key = "money.free_ask_group" if asked else "money.free_offer_group"
        free = button(t(locale, "money.free_enable"), FreeModels(action="enable"))
        keyboard = markup(buy_button(locale, group=True), free)
    else:
        key = "money.free_ask" if asked else "money.free_offer"
        free = button(t(locale, "money.free_consent"), FreeModels(action="consent"))
        keyboard = markup([buy_button(locale, group=False), free])
    return t(locale, key, time=refill), keyboard


async def offer_due(db: Database, chat_id: int, user_id: int, now: datetime | None = None) -> bool:
    """True the first time today (UTC) this person hits the wall in this chat; records it."""
    day = (now or datetime.now(UTC)).date()
    statement = (
        insert(ChatMember)
        .values(chat_id=chat_id, user_id=user_id, offered_on=day)
        .on_conflict_do_update(
            index_elements=["chat_id", "user_id"],
            set_={"offered_on": day},
            where=or_(ChatMember.offered_on.is_(None), ChatMember.offered_on < day),
        )
        .returning(ChatMember.offered_on)
    )
    async with db.transaction() as session:
        return (await session.execute(statement)).first() is not None


async def answer_unfunded(
    db: Database, bot: Bot, message: Message, chat: Chat, target: Target, locale: str
) -> None:
    """Nobody can pay for this ask: the offer; in groups once per person per UTC day, later just a 🙈 on it."""
    private = chat.type == "private"
    if private or await offer_due(db, chat.id, message.from_user.id if message.from_user else 0):
        text, keyboard = offer(locale, group=not private)
        with contextlib.suppress(TelegramAPIError):
            await send_markdown(bot, target, text, reply_markup=keyboard)
    elif message.message_id:
        with contextlib.suppress(TelegramAPIError):
            await bot.set_message_reaction(
                chat_id=chat.id, message_id=message.message_id, reaction=[ReactionTypeEmoji(emoji="🙈")]
            )


async def note_empty_wallet(
    bot: Bot, funder: Funder, chat: Chat, funds: Funds, charged: int, thread_id: int | None, locale: str
) -> None:
    """After a charge to a group's wallet: one silent line once it can no longer pay a turn, per depletion."""
    if not charged or funds.wallet_id is None or chat.type == "private":
        return
    try:
        if not await funder.ran_dry(funds.wallet_id):
            return
        public = chat.locale or locale
        await send_markdown(
            bot,
            Target(chat.id, thread_id),
            t(public, "money.wallet_empty"),
            reply_markup=markup(top_up_button(public)),
            silent=True,
        )
    except Exception:  # noqa: BLE001 - a notice never fails the run that already answered
        log.info("empty wallet notice not delivered")


def build_router() -> Router:
    router = Router(name="free-models")

    @on_press(router, FreeModels, F.action.in_({"consent", "enable"}))
    async def on_free(press: Press, data: FreeModels, db: Database, runtime: Answers) -> None:
        message, bot = press.message, press.bot
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
