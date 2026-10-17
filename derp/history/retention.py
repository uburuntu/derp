"""Forgetting: retention expiry and privacy requests over chat knowledge.

- Raw history (messages with their media, reactions, polls and votes, plus
  membership events) lives for the chat's retention: ``chats.retention_days``,
  else the private / group default.
- Digests live ``Retention.digest_days``; a stale digest whose messages are
  gone can't be rebuilt and is dropped.
- ``forget_message`` / ``forget_member``: a person's own message, or all of
  theirs in one chat. Rows are tombstoned and scrubbed (text, files, notes on
  files, reactions, polls; quiz points and titles of the day go too), and every
  digest whose day range covers them is deleted, to be rebuilt on demand
  without them.
- ``purge_ambient``: the chat's context was switched off — every ambient row
  goes, and with it every digest of the chat.
"""

from datetime import datetime, timedelta

from sqlalchemy import (
    ColumnElement,
    Integer,
    Select,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    null,
    select,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from derp.config import Retention
from derp.history.models import (
    Chat,
    ChatMember,
    DailyTitle,
    Digest,
    GameScore,
    MemberEvent,
    Message,
    MessageMedia,
    Poll,
    PollVote,
    Reaction,
    ReactionCount,
)
from derp.ops.models import Feedback


def retention_days(retention: Retention) -> ColumnElement[int]:
    """The chat's retention in days (correlates with ``Chat``)."""
    return func.coalesce(
        Chat.retention_days,
        case((Chat.type == "private", retention.private_days), else_=retention.group_days),
    )


def expired(
    chat_id: InstrumentedAttribute[int],
    moment: InstrumentedAttribute[datetime],
    retention: Retention,
    now: datetime,
) -> ColumnElement[bool]:
    """Rows older than their chat's retention."""
    cutoff = literal(now) - func.make_interval(0, 0, 0, cast(retention_days(retention), Integer))
    return exists(select(Chat.id).where(Chat.id == chat_id, moment < cutoff))


async def _drop_attached(session: AsyncSession, ids: Select[int, int]) -> None:
    """Reactions, reaction totals, polls and votes hanging off the selected (chat, message) pairs."""
    polls = select(Poll.id).where(tuple_(Poll.chat_id, Poll.message_id).in_(ids))
    await session.execute(delete(PollVote).where(PollVote.poll_id.in_(polls)))
    await session.execute(delete(Poll).where(tuple_(Poll.chat_id, Poll.message_id).in_(ids)))
    await session.execute(delete(Reaction).where(tuple_(Reaction.chat_id, Reaction.message_id).in_(ids)))
    await session.execute(
        delete(ReactionCount).where(tuple_(ReactionCount.chat_id, ReactionCount.message_id).in_(ids))
    )


async def purge_expired(session: AsyncSession, retention: Retention, now: datetime) -> int:
    """Delete everything past retention; returns the number of messages deleted."""
    old = expired(Message.chat_id, Message.date, retention, now)
    await _drop_attached(session, select(Message.chat_id, Message.message_id).where(old))
    deleted = await session.execute(delete(Message).where(old))  # media rows cascade
    await session.execute(
        delete(MemberEvent).where(expired(MemberEvent.chat_id, MemberEvent.at, retention, now))
    )
    await session.execute(
        delete(Digest).where(Digest.day < (now - timedelta(days=retention.digest_days)).date())
    )
    survivors = select(Message.message_id).where(
        Message.chat_id == Digest.chat_id,
        Message.message_id.between(Digest.first_message_id, Digest.last_message_id),
        Message.deleted_at.is_(None),
    )
    await session.execute(delete(Digest).where(Digest.stale, ~exists(survivors)))
    return int(getattr(deleted, "rowcount", 0) or 0)


async def _scrub(session: AsyncSession, chat_id: int, where: ColumnElement[bool], now: datetime) -> int:
    ids = select(Message.chat_id, Message.message_id).where(Message.chat_id == chat_id, where)
    await _drop_attached(session, ids)
    await session.execute(
        delete(MessageMedia).where(tuple_(MessageMedia.chat_id, MessageMedia.message_id).in_(ids))
    )
    covered = select(Message.message_id).where(
        Message.chat_id == chat_id,
        where,
        Message.message_id.between(Digest.first_message_id, Digest.last_message_id),
    )
    # Summaries that may quote what goes are deleted, not just marked stale: rebuilt on demand without it.
    await session.execute(delete(Digest).where(Digest.chat_id == chat_id, exists(covered)))
    scrubbed = await session.execute(
        update(Message)
        .where(Message.chat_id == chat_id, where)
        .values(
            deleted_at=func.coalesce(Message.deleted_at, now),
            text=None,
            extra_text=None,
            entities=null(),
            quote=None,
            forward_origin=null(),
            payload={},
            raw=null(),
        )
    )
    return int(getattr(scrubbed, "rowcount", 0) or 0)


async def forget_message(session: AsyncSession, chat_id: int, message_id: int, now: datetime) -> bool:
    return await _scrub(session, chat_id, Message.message_id == message_id, now) > 0


async def forget_member(session: AsyncSession, chat_id: int, user_id: int, now: datetime) -> int:
    """Everything of one person in one chat; returns the number of their messages forgotten."""
    count = await _scrub(session, chat_id, Message.user_id == user_id, now)
    await session.execute(delete(Reaction).where(Reaction.chat_id == chat_id, Reaction.actor_id == user_id))
    polls = select(Poll.id).where(Poll.chat_id == chat_id)
    await session.execute(delete(PollVote).where(PollVote.voter_id == user_id, PollVote.poll_id.in_(polls)))
    await session.execute(
        delete(ChatMember).where(ChatMember.chat_id == chat_id, ChatMember.user_id == user_id)
    )
    await session.execute(
        delete(MemberEvent).where(MemberEvent.chat_id == chat_id, MemberEvent.user_id == user_id)
    )
    await session.execute(delete(GameScore).where(GameScore.chat_id == chat_id, GameScore.user_id == user_id))
    await session.execute(
        delete(DailyTitle).where(DailyTitle.chat_id == chat_id, DailyTitle.user_id == user_id)
    )
    await session.execute(delete(Feedback).where(Feedback.chat_id == chat_id, Feedback.user_id == user_id))
    return count


async def purge_ambient(session: AsyncSession, chat_id: int) -> int:
    """Context switched off: delete the chat's ambient messages and all its digests."""
    ambient = (Message.chat_id == chat_id) & (Message.capture == "ambient")
    await _drop_attached(session, select(Message.chat_id, Message.message_id).where(ambient))
    deleted = await session.execute(delete(Message).where(ambient))
    await session.execute(delete(Digest).where(Digest.chat_id == chat_id))
    return int(getattr(deleted, "rowcount", 0) or 0)
