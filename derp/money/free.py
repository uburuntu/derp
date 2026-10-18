"""Free models (the last rung, only with consent), the waiting question, and the money notices of a run.

Free OpenRouter models are not zero-data-retention: their providers may store
prompts. So a person opts in once for their private chats (recorded with the
policy text version they saw), and in groups an admin turns them on for the
whole chat, which posts a short public notice.

An ask nobody could pay for is the person's waiting question in that chat
(``chat_members.waiting_message_id``, the newest one wins). For
``waiting_minutes`` it can be resumed, once: right after consenting to (or an
admin enabling) free models, or with the Continue button a purchase offers the
buyer for their own waiting question in the chat and topic the purchase was for.
Each resume claims the question (``claim``), so presses never answer it twice.

The out-of-Derpcoins offer says when the free daily Derpcoins refill (a date-time
entity, so each person sees their own clock) and leads with buying (``buy_button``).
In groups it is shown to each person at most once per chat per UTC day
(``chat_members.offered_on``); later unfunded asks get a 🙈 reaction. After a charge
leaves a chat wallet unable to pay a turn, one silent line says so, once per depletion.
"""

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message, ReactionTypeEmoji
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun
from derp.billing.menu import buy_button, top_up_button
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, ChatMember, User
from derp.history.models import Message as StoredMessage
from derp.locales import t
from derp.memory.notes import is_admin
from derp.money.funding import Funder, Funds, day_end
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, send_markdown

POLICY_VERSION = "2026-10"  # bump when the free-model notice changes: consent is asked again
log = logging.getLogger("derp.money")


class Unblock(CallbackData, prefix="free"):
    """Presses that unblock a waiting question; each then answers it (``resume``)."""

    action: str  # consent (free models, a person) | enable (free models, an admin) | continue (after buying)


class Answers(Protocol):
    """What an unblocking press needs from the runtime: answering the question that ran out of Derpcoins."""

    async def answer(self, message: Message, chat: Chat, *, again: bool) -> object: ...


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
        free = button(t(locale, "money.free_enable"), Unblock(action="enable"))
        keyboard = markup(buy_button(locale, group=True), free)
    else:
        key = "money.free_ask" if asked else "money.free_offer"
        free = button(t(locale, "money.free_consent"), Unblock(action="consent"))
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
    """Nobody can pay for this ask: it waits; the offer, in groups once per person per UTC day, later a 🙈 on it."""
    private = chat.type == "private"
    asker = message.from_user.id if message.from_user else 0
    if message.message_id:
        await wait(db, chat.id, asker, message.message_id)
    if private or await offer_due(db, chat.id, asker):
        text, keyboard = offer(locale, group=not private)
        with contextlib.suppress(TelegramAPIError):
            await send_markdown(bot, target, text, reply_markup=keyboard)
    elif message.message_id:
        with contextlib.suppress(TelegramAPIError):
            await bot.set_message_reaction(
                chat_id=chat.id, message_id=message.message_id, reaction=[ReactionTypeEmoji(emoji="🙈")]
            )


async def wait(db: Database, chat_id: int, user_id: int, message_id: int) -> None:
    """Remember this ask as the person's waiting question in the chat (replacing an older one)."""
    now = datetime.now(UTC)
    statement = insert(ChatMember).values(
        chat_id=chat_id, user_id=user_id, waiting_message_id=message_id, waiting_at=now
    )
    async with db.transaction() as session:
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["chat_id", "user_id"],
                set_={"waiting_message_id": message_id, "waiting_at": now},
            )
        )


async def waiting_question(
    session: AsyncSession, settings: Settings, chat_id: int, user_id: int
) -> StoredMessage | None:
    """The person's waiting question in this chat: recent, still stored, and no newer ask of theirs answered."""
    member = await session.get(ChatMember, (chat_id, user_id))
    since = datetime.now(UTC) - timedelta(minutes=settings.waiting_minutes)
    if member is None or member.waiting_message_id is None or member.waiting_at is None:
        return None
    if member.waiting_at <= since:
        return None
    answered_since = await session.scalar(  # a newer ask of theirs got an answer: this one is behind them
        select(AgentRun.id).where(
            AgentRun.chat_id == chat_id,
            AgentRun.user_id == user_id,
            AgentRun.trigger_message_id > member.waiting_message_id,
        )
    )
    question = await session.get(StoredMessage, (chat_id, member.waiting_message_id))
    return (
        question if answered_since is None and question is not None and question.deleted_at is None else None
    )


async def claim(db: Database, settings: Settings, chat_id: int, user_id: int, message_id: int) -> bool | None:
    """Take this waiting question to answer it, once.

    True when it still waits; False when it went stale (expired, or a newer ask came); None when
    there is nothing left to take (another press took it).
    """
    async with db.transaction() as session:
        member = await session.get(ChatMember, (chat_id, user_id), with_for_update=True)
        if member is None or member.waiting_message_id is None:
            return None
        waiting = await waiting_question(session, settings, chat_id, user_id)
        if member.waiting_message_id == message_id:
            member.waiting_message_id = None
        return waiting is not None and waiting.message_id == message_id


async def offer_continue(
    db: Database,
    bot: Bot,
    settings: Settings,
    *,
    chat_id: int,
    thread_id: int | None,
    user_id: int,
    locale: str,
) -> None:
    """After a purchase: the buyer's own question waiting in that chat and topic gets a Continue button."""
    async with db.session() as session:
        waiting = await waiting_question(session, settings, chat_id, user_id)
    if waiting is None or waiting.thread_id != thread_id:
        return
    again = markup(button(t(locale, "money.continue"), Unblock(action="continue")))
    target = Target(chat_id, thread_id, waiting.message_id)
    with contextlib.suppress(TelegramAPIError):  # the purchase is recorded; the button is a courtesy
        await send_markdown(bot, target, t(locale, "money.continue_offer"), reply_markup=again)


async def resume(press: Press, db: Database, settings: Settings, runtime: Answers) -> bool | None:
    """Answer the waiting question the pressed message replies to, if it still waits (see ``claim``)."""
    trigger = press.message.reply_to_message  # the question that ran out of Derpcoins
    if trigger is None or trigger.from_user is None:
        return None
    taken = await claim(db, settings, press.chat_id, trigger.from_user.id, trigger.message_id)
    if taken:
        async with db.session() as session:
            chat = await session.get(Chat, press.chat_id)
        if chat is not None:
            await runtime.answer(trigger, chat, again=True)  # older than what the asker said since
    return taken


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

    @on_press(router, Unblock, F.action.in_({"consent", "enable", "continue"}))
    async def on_unblock(
        press: Press, data: Unblock, db: Database, settings: Settings, runtime: Answers
    ) -> None:
        bot = press.bot
        if data.action == "continue":
            asker = press.message.reply_to_message.from_user if press.message.reply_to_message else None
            if asker is None or asker.id != press.user_id:  # a patron's purchase never replays others' asks
                return await press.deny("money.continue_not_yours")
            await press.strip()
            if await resume(press, db, settings, runtime) is False:
                await press.edit(t(press.locale, "money.continue_stale"))
            return None
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
        await resume(press, db, settings, runtime)
        return None

    return router
