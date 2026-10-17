"""The one compact notice a group gets: when Derp joins, or on the first invocation of a chat
that predates it. It says how to summon Derp, whether Derp can see the chat (honest
"mentions only" when it can't), where settings live, and offers an optional admin upgrade.
"""

from datetime import UTC, datetime

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatMemberUpdated, InlineKeyboardMarkup

from derp.config import Settings
from derp.db.engine import Database
from derp.history.members import ADMIN_STATUSES, context_state
from derp.history.members import on_my_chat_member as record_membership
from derp.history.models import Chat
from derp.history.retention import kept_days
from derp.locales import day_count, resolve, t
from derp.telegram.bot import FALLBACK_USERNAME
from derp.telegram.buttons import link, markup
from derp.telegram.sender import Target, send_markdown

GROUP_TYPES = {"group", "supergroup"}


async def context_line(bot: Bot, settings: Settings, chat: Chat, locale: str) -> tuple[str, str | None]:
    """What Derp sees here, in a sentence (with the retention when it reads the chat), and the make-me-admin link."""
    context = await context_state(bot, chat)
    kept = day_count(locale, kept_days(chat, settings.retention))
    return t(locale, f"onboarding.context_{context.state}", days=kept), context.admin_url


async def notice(
    bot: Bot, settings: Settings, chat: Chat, locale: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    me = await bot.me()
    context, admin_url = await context_line(bot, settings, chat, locale)
    text = "\n\n".join(
        [
            t(locale, "onboarding.title"),
            t(locale, "onboarding.summon", username=me.username or FALLBACK_USERNAME),
            context,
            t(locale, "onboarding.settings"),
        ]
    )
    return text, markup(link(t(locale, "onboarding.make_admin"), admin_url) if admin_url else None)


async def ensure_onboarded(
    bot: Bot, db: Database, settings: Settings, chat_id: int, language_code: str | None
) -> None:
    """Send the notice once per group; the first person's language becomes the chat locale if unset."""
    async with db.transaction() as session:
        chat = await session.get(Chat, chat_id, with_for_update=True)
        if chat is None or chat.type not in GROUP_TYPES or chat.onboarded_at is not None:
            return
        chat.locale = chat.locale or resolve(language_code)
        chat.onboarded_at = datetime.now(UTC)
    text, markup = await notice(bot, settings, chat, chat.locale or resolve(None))
    try:
        await send_markdown(bot, Target(chat_id), text, reply_markup=markup)
    except TelegramBadRequest, TelegramForbiddenError:  # surely not delivered; anything else might have been
        async with db.transaction() as session:  # try again on the next invocation
            if (row := await session.get(Chat, chat_id)) is not None:
                row.onboarded_at = None


async def on_my_chat_member(update: ChatMemberUpdated, db: Database, bot: Bot, settings: Settings) -> None:
    await record_membership(update, db, bot)
    joined = update.old_chat_member.status in {"left", "kicked"} and update.new_chat_member.status in (
        ADMIN_STATUSES | {"member"}
    )
    if joined and update.chat.type in GROUP_TYPES:
        await ensure_onboarded(bot, db, settings, update.chat.id, update.from_user.language_code)


def build_router() -> Router:
    router = Router(name="onboarding")
    router.my_chat_member.register(on_my_chat_member)
    return router
