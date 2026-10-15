"""Reactions on stored messages.

Telegram delivers ``message_reaction`` (per actor) and ``message_reaction_count``
(anonymous totals) only to admins, so a chat where Derp is not an admin simply
has no reaction rows. Reactions on messages Derp did not store are dropped:
they follow the message's retention and context policy.
"""

from aiogram import Router
from aiogram.types import (
    MessageReactionCountUpdated,
    MessageReactionUpdated,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReactionTypePaid,
)
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from derp.db.engine import Database
from derp.history.capture import is_stored, upsert_chat, upsert_user
from derp.history.models import Reaction, ReactionCount


def reaction_key(reaction: ReactionTypeEmoji | ReactionTypeCustomEmoji | ReactionTypePaid) -> str:
    match reaction:
        case ReactionTypeEmoji():
            return reaction.emoji
        case ReactionTypeCustomEmoji():
            return f"custom:{reaction.custom_emoji_id}"
        case ReactionTypePaid():
            return "paid"


async def on_reaction(update: MessageReactionUpdated, db: Database) -> None:
    actor = update.user or update.actor_chat
    if actor is None:
        return
    chat_id, message_id = update.chat.id, update.message_id
    async with db.transaction() as session:
        if not await is_stored(session, chat_id, message_id):
            return
        await upsert_chat(session, update.chat)
        if update.user is not None:
            await upsert_user(session, update.user)
        await session.execute(
            delete(Reaction).where(
                Reaction.chat_id == chat_id, Reaction.message_id == message_id, Reaction.actor_id == actor.id
            )
        )
        keys = dict.fromkeys(reaction_key(r) for r in update.new_reaction)
        for key in keys:
            await session.execute(
                insert(Reaction)
                .values(
                    chat_id=chat_id,
                    message_id=message_id,
                    actor_id=actor.id,
                    reaction=key,
                    actor_is_chat=update.user is None,
                )
                .on_conflict_do_nothing()
            )


async def on_reaction_count(update: MessageReactionCountUpdated, db: Database) -> None:
    chat_id, message_id = update.chat.id, update.message_id
    async with db.transaction() as session:
        if not await is_stored(session, chat_id, message_id):
            return
        await session.execute(
            delete(ReactionCount).where(
                ReactionCount.chat_id == chat_id, ReactionCount.message_id == message_id
            )
        )
        for item in update.reactions:
            session.add(
                ReactionCount(
                    chat_id=chat_id,
                    message_id=message_id,
                    reaction=reaction_key(item.type),
                    count=item.total_count,
                )
            )


def build_router() -> Router:
    router = Router(name="reactions")
    router.message_reaction.register(on_reaction)
    router.message_reaction_count.register(on_reaction_count)
    return router
