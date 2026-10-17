"""The one compact notice a group gets: when Derp joins, or on the first invocation of a chat
that predates it. It says how to summon Derp, whether Derp can see the chat (honest
"mentions only" when it can't), where settings live, and offers an optional admin upgrade.
"""

from datetime import UTC, datetime

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup

from derp.db.engine import Database
from derp.history.members import ADMIN_STATUSES
from derp.history.members import on_my_chat_member as record_membership
from derp.history.models import Chat
from derp.locales import resolve, t
from derp.telegram.sender import Target, send_markdown

GROUP_TYPES = {"group", "supergroup"}
ADMIN_LINK_RIGHTS = "invite_users"  # any admin role unlocks reaction and member updates; ask for the mildest


async def can_read_group(bot: Bot, chat: Chat) -> bool:
    """Whether Telegram delivers ordinary (non-mention) group messages to Derp here."""
    me = await bot.me()
    return bool(me.can_read_all_group_messages) or (chat.bot_status or "") in ADMIN_STATUSES


async def sees_everything(bot: Bot, chat: Chat) -> bool:
    """Ambient context is on and Telegram actually delivers ordinary group messages."""
    return chat.ambient and await can_read_group(bot, chat)


async def context_key(bot: Bot, chat: Chat) -> str:
    if not chat.ambient:
        return "onboarding.context_off"
    return "onboarding.context_on" if await can_read_group(bot, chat) else "onboarding.context_needs_admin"


async def notice(bot: Bot, chat: Chat, locale: str) -> tuple[str, InlineKeyboardMarkup | None]:
    me = await bot.me()
    context = await context_key(bot, chat)
    days = chat.retention_days or 30
    text = "\n\n".join(
        [
            t(locale, "onboarding.title"),
            t(locale, "onboarding.summon", username=me.username or "DerpRobot"),
            t(locale, context, days=days),
            t(locale, "onboarding.settings"),
        ]
    )
    if (chat.bot_status or "") in ADMIN_STATUSES or not me.username:
        return text, None
    link = f"https://t.me/{me.username}?startgroup=derp&admin={ADMIN_LINK_RIGHTS}"
    button = InlineKeyboardButton(text=t(locale, "onboarding.make_admin"), url=link)
    return text, InlineKeyboardMarkup(inline_keyboard=[[button]])


async def ensure_onboarded(bot: Bot, db: Database, chat_id: int, language_code: str | None) -> None:
    """Send the notice once per group; the first person's language becomes the chat locale if unset."""
    async with db.transaction() as session:
        chat = await session.get(Chat, chat_id, with_for_update=True)
        if chat is None or chat.type not in GROUP_TYPES or chat.onboarded_at is not None:
            return
        chat.locale = chat.locale or resolve(language_code)
        chat.onboarded_at = datetime.now(UTC)
    text, markup = await notice(bot, chat, chat.locale or resolve(None))
    try:
        await send_markdown(bot, Target(chat_id), text, reply_markup=markup)
    except TelegramBadRequest, TelegramForbiddenError:  # surely not delivered; anything else might have been
        async with db.transaction() as session:  # try again on the next invocation
            if (row := await session.get(Chat, chat_id)) is not None:
                row.onboarded_at = None


async def on_my_chat_member(update: ChatMemberUpdated, db: Database, bot: Bot) -> None:
    await record_membership(update, db, bot)
    joined = update.old_chat_member.status in {"left", "kicked"} and update.new_chat_member.status in (
        ADMIN_STATUSES | {"member"}
    )
    if joined and update.chat.type in GROUP_TYPES:
        await ensure_onboarded(bot, db, update.chat.id, update.from_user.language_code)


def build_router() -> Router:
    router = Router(name="onboarding")
    router.my_chat_member.register(on_my_chat_member)
    return router
