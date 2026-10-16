"""Inline mode and guest mode: Derp outside the chats it lives in.

Inline (``@DerpRobot question`` in any chat): the query gets one placeholder
result; when the person sends it, the answer is edited into that message.
Guest mode (a mention in a chat Derp isn't a member of): one reply through
``answerGuestQuery``. Both are paid from the asker's own funding, see no chat
history, and store nothing but accounting. Charged only once the answer is in.
"""

import logging
import uuid

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputRichMessage,
    InputRichMessageContent,
    InputTextMessageContent,
    Message,
)

from derp.agent.runtime import Runtime
from derp.db.engine import Database
from derp.locales import resolve, t
from derp.ops.owner import is_blocked
from derp.telegram.sender import sanitize_untrusted

log = logging.getLogger("derp.inline")
QUERY_LIMIT = 256  # Telegram caps inline queries at 256 characters


def _waiting_markup(locale: str) -> InlineKeyboardMarkup:
    # A keyboard is what makes Telegram report the sent message's inline_message_id.
    button = InlineKeyboardButton(text=t(locale, "inline.ask_too"), switch_inline_query_current_chat="")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


async def _blocked(db: Database, user_id: int) -> bool:
    async with db.session() as session:
        return await is_blocked(session, user_id, user_id)


async def on_inline_query(query: InlineQuery, bot: Bot, db: Database) -> None:
    locale = resolve(query.from_user.language_code)
    text = query.query.strip()
    if await _blocked(db, query.from_user.id):
        await bot.answer_inline_query(query.id, results=[], cache_time=0, is_personal=True)
        return
    button = InlineQueryResultsButton(text=t(locale, "inline.get_coins"), start_parameter="buy")
    if not text:
        await bot.answer_inline_query(query.id, results=[], cache_time=0, is_personal=True, button=button)
        return
    placeholder = InlineQueryResultArticle(
        id=uuid.uuid4().hex,
        title=t(locale, "inline.title"),
        description=text[:100],
        input_message_content=InputTextMessageContent(message_text=t(locale, "inline.thinking", query=text)),
        reply_markup=_waiting_markup(locale),
    )
    await bot.answer_inline_query(
        query.id, results=[placeholder], cache_time=0, is_personal=True, button=button
    )


async def on_chosen(result: ChosenInlineResult, bot: Bot, runtime: Runtime) -> None:
    if not result.inline_message_id:
        return
    locale = resolve(result.from_user.language_code)
    detached = await runtime.answer_detached(
        asker=result.from_user,
        prompt=result.query[:QUERY_LIMIT],
        trigger="inline",
        operation_id=f"inline:{result.inline_message_id}",
        where="through inline mode into some chat",
    )
    question = f"❓ {sanitize_untrusted(result.query[:QUERY_LIMIT])}\n\n"
    if detached is None or not detached.text:
        text = question + t(locale, "inline.unfunded" if detached is None else "inline.failed")
    else:
        text = question + sanitize_untrusted(detached.text)
    delivered = False
    try:
        await bot.edit_message_text(
            inline_message_id=result.inline_message_id, rich_message=InputRichMessage(markdown=text)
        )
        delivered = bool(detached and detached.text)
    except TelegramAPIError as error:
        log.info("inline answer not delivered: %s", type(error).__name__)
    finally:
        if detached is not None:
            await runtime.settle_detached(detached, delivered=delivered)


async def on_guest(message: Message, bot: Bot, runtime: Runtime, db: Database) -> None:
    if not message.guest_query_id or message.from_user is None or await _blocked(db, message.from_user.id):
        return
    locale = resolve(message.from_user.language_code)
    detached = await runtime.answer_detached(
        asker=message.from_user,
        prompt=(message.text or message.caption or "")[:4000],
        trigger="guest",
        operation_id=f"guest:{message.guest_query_id}",
        where="as a guest into a chat Derp isn't a member of",
    )
    if detached is None or not detached.text:
        text = t(locale, "inline.unfunded" if detached is None else "inline.failed")
    else:
        text = sanitize_untrusted(detached.text)
    reply = InlineQueryResultArticle(
        id=uuid.uuid4().hex,
        title="Derp",
        input_message_content=InputRichMessageContent(rich_message=InputRichMessage(markdown=text)),
    )
    delivered = False
    try:
        await bot.answer_guest_query(guest_query_id=message.guest_query_id, result=reply)
        delivered = bool(detached and detached.text)
    except TelegramAPIError as error:
        log.info("guest answer not delivered: %s", type(error).__name__)
    finally:
        if detached is not None:
            await runtime.settle_detached(detached, delivered=delivered)


def build_router() -> Router:
    router = Router(name="inline")
    router.inline_query.register(on_inline_query)
    router.chosen_inline_result.register(on_chosen)
    router.guest_message.register(on_guest)
    return router
