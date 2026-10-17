"""Members, membership events and the chat profile snapshot.

Message senders are tracked from every captured message; join/leave service
messages and (when Derp is an admin) ``chat_member`` updates produce events.
The profile (description, pinned message, admins, reactions, member count)
comes from ``getChat`` on joins, title changes and a daily refresh.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated
from aiogram.types import Message as TgMessage
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.db.engine import Database
from derp.history.capture import upsert_chat, upsert_user
from derp.history.models import Chat, ChatMember, MemberEvent, Message
from derp.scheduler.jobs import pause_jobs

log = logging.getLogger("derp.members")
ADMIN_STATUSES = {"creator", "administrator"}
NAMES_KEPT = 5
PROFILE_MAX_AGE = timedelta(hours=24)
DUPLICATE_WINDOW = 60  # seconds


async def touch_member(
    session: AsyncSession, chat_id: int, user_id: int, name: str, *, counted: bool
) -> None:
    statement = insert(ChatMember).values(
        chat_id=chat_id, user_id=user_id, names=[name], message_count=1 if counted else 0
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ChatMember.chat_id, ChatMember.user_id],
            set_={
                "last_seen": func.now(),
                "message_count": ChatMember.message_count + (1 if counted else 0),
                "status": func.coalesce(
                    func.nullif(func.nullif(ChatMember.status, "left"), "kicked"), "member"
                ),
            },
        )
    )
    member = await session.get(ChatMember, (chat_id, user_id))
    if member is not None and name and (not member.names or member.names[-1] != name):
        member.names = [*[n for n in member.names if n != name], name][-NAMES_KEPT:]


async def record_event(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    kind: str,
    *,
    actor_id: int | None = None,
    status: str | None = None,
) -> None:
    # While Derp is an admin, a join/leave arrives twice (service message + chat_member update), and
    # service messages can be redelivered: the same event within a minute is recorded once.
    duplicate = await session.scalar(
        select(MemberEvent.id).where(
            MemberEvent.chat_id == chat_id,
            MemberEvent.user_id == user_id,
            MemberEvent.kind == kind,
            MemberEvent.at > func.now() - timedelta(seconds=DUPLICATE_WINDOW),
        )
    )
    if duplicate is None:
        session.add(MemberEvent(chat_id=chat_id, user_id=user_id, kind=kind, actor_id=actor_id))
    if kind in ("leave", "ban"):  # their scheduled prompts would keep spending in a chat they left
        await pause_jobs(session, chat_id, user_id)
    if status is not None:
        statement = insert(ChatMember).values(chat_id=chat_id, user_id=user_id, status=status)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[ChatMember.chat_id, ChatMember.user_id],
                set_={"status": status, "last_seen": func.now()},
            )
        )


async def track_message(session: AsyncSession, message: TgMessage, *, stored: bool) -> None:
    """Membership facts carried by a captured message."""
    if message.chat.type == "private":
        return
    chat_id = message.chat.id
    actor = message.from_user.id if message.from_user else None
    if message.from_user is not None and not message.from_user.is_bot:
        await touch_member(
            session, chat_id, message.from_user.id, message.from_user.full_name, counted=stored
        )
    for user in message.new_chat_members or []:
        await record_event(session, chat_id, user.id, "join", actor_id=actor, status="member")
    if (left := message.left_chat_member) is not None:
        await record_event(session, chat_id, left.id, "leave", actor_id=actor, status="left")


def classify_change(old: str, new: str) -> str | None:
    if old == new:
        return None
    if new in ADMIN_STATUSES and old not in ADMIN_STATUSES:
        return "promote"
    if old in ADMIN_STATUSES and new not in ADMIN_STATUSES and new in {"member", "restricted"}:
        return "demote"
    if new == "kicked":
        return "ban"
    if old == "kicked":
        return "unban" if new == "left" else "join"
    if new == "left":
        return "leave"
    if new == "restricted" and old == "member":
        return "restrict"
    if old in {"left", "kicked"} or new in {"member", "restricted"}:
        return "join"
    return None


async def on_chat_member(update: ChatMemberUpdated, db: Database) -> None:
    old, new = update.old_chat_member, update.new_chat_member
    kind = classify_change(old.status, new.status)
    async with db.transaction() as session:
        await upsert_chat(session, update.chat)
        await upsert_user(session, new.user)
        if kind is not None:
            await record_event(
                session,
                update.chat.id,
                new.user.id,
                kind,
                actor_id=update.from_user.id,
                status=str(new.status),
            )
        if (member := await session.get(ChatMember, (update.chat.id, new.user.id))) is not None:
            member.tag = getattr(new, "custom_title", None) or getattr(new, "tag", None)


async def on_my_chat_member(update: ChatMemberUpdated, db: Database, bot: Bot) -> None:
    async with db.transaction() as session:
        chat = await upsert_chat(session, update.chat)
        chat.bot_status = str(update.new_chat_member.status)
        if update.new_chat_member.status in ("left", "kicked"):
            await pause_jobs(session, update.chat.id)
    if update.new_chat_member.status in ADMIN_STATUSES | {"member"}:
        await refresh_profile(bot, db, update.chat.id)


async def refresh_profile(bot: Bot, db: Database, chat_id: int) -> None:
    """Snapshot getChat + admins into ``chats.profile``; admins also update member rows."""
    try:
        info = await bot.get_chat(chat_id)
        admins = await bot.get_chat_administrators(chat_id) if info.type != "private" else []
        count = await bot.get_chat_member_count(chat_id) if info.type != "private" else None
    except TelegramAPIError as error:
        log.info("profile refresh skipped for %s: %s", chat_id, type(error).__name__)
        return
    profile: dict[str, Any] = {
        "description": info.description,
        "pinned_message_id": info.pinned_message.message_id if info.pinned_message else None,
        "pinned_text": (info.pinned_message.text or info.pinned_message.caption)
        if info.pinned_message
        else None,
        "available_reactions": [
            r.model_dump(mode="json", exclude_none=True) for r in info.available_reactions or []
        ]
        if info.available_reactions is not None
        else None,
        "member_count": count,
        "message_auto_delete_time": info.message_auto_delete_time,
        "admins": [
            {
                "id": a.user.id,
                "name": a.user.full_name,
                "status": str(a.status),
                "title": getattr(a, "custom_title", None),
            }
            for a in admins
            if not a.user.is_bot
        ],
        "refreshed_at": datetime.now(UTC).isoformat(),
    }
    async with db.transaction() as session:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            return
        chat.title = info.title or chat.title
        chat.profile = {k: v for k, v in profile.items() if v is not None}
        for admin in admins:
            if admin.user.is_bot:
                continue
            await upsert_user(session, admin.user)
            statement = insert(ChatMember).values(
                chat_id=chat_id, user_id=admin.user.id, status=str(admin.status)
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ChatMember.chat_id, ChatMember.user_id],
                    set_={"status": str(admin.status)},
                )
            )


async def refresh_stale_profiles(bot: Bot, db: Database, *, limit: int = 50) -> None:
    """Daily: refresh profiles of chats active in the last day whose snapshot is older than a day."""
    since = datetime.now(UTC) - PROFILE_MAX_AGE
    async with db.session() as session:
        active = select(Message.chat_id).where(Message.date >= since).distinct().scalar_subquery()
        chats = await session.scalars(
            select(Chat.id)
            .where(Chat.id.in_(active), Chat.type != "private")
            .where(func.coalesce(Chat.profile["refreshed_at"].astext, "") < since.isoformat())
            .limit(limit)
        )
        ids = list(chats)
    for chat_id in ids:
        await refresh_profile(bot, db, chat_id)


def build_router() -> Router:
    """``chat_member`` updates; Derp's own ``my_chat_member`` goes through onboarding, which calls
    ``on_my_chat_member`` first."""
    router = Router(name="members")
    router.chat_member.register(on_chat_member)
    return router


async def can_read_group(bot: Bot, chat: Chat) -> bool:
    """Whether Telegram delivers ordinary (non-mention) group messages to Derp here."""
    me = await bot.me()
    return bool(me.can_read_all_group_messages) or (chat.bot_status or "") in ADMIN_STATUSES
