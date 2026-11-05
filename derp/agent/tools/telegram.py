"""Telegram-native outputs (dice, stickers, reactions) and actions on messages (edit, delete, pin).

Every output may reply to any stored message of this chat (validated here); the default is the
trigger. Tools may send up to ``visible_outputs - 1`` items so the final text answer always has a slot;
budget, send and record go through ``derp.agent.outputs.deliver``. Output tools run sequentially: the
budget check and the send must not interleave, and outputs keep the order the model chose. Polls are
``games.post_polls``.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReactionTypeEmoji, ReplyParameters
from pydantic import Field
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.deps import RunDeps
from derp.agent.outputs import Refused, check_budget, deliver, unconfirmed
from derp.agent.toolsets import ALL_TIERS, Family
from derp.history.digests import mark_stale
from derp.history.links import message_links
from derp.history.models import Message, MessageMedia, Sticker
from derp.memory.notes import is_admin
from derp.telegram.policy import AMBIGUOUS
from derp.telegram.sender import rich, sanitize_untrusted
from derp.telegram.text import plain_emoji

toolset = FunctionToolset[RunDeps]()
ops = FunctionToolset[RunDeps]()
DiceEmoji = Literal["🎲", "🎯", "🏀", "⚽", "🎳", "🎰"]
REACTIONS = (
    "❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊",
    "🤡", "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋",
    "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃",
    "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷", "🤷‍♀", "😡",
)  # fmt: skip


MessageId = Annotated[int, Field(ge=1)]


async def reply_target(deps: RunDeps, reply_to: int | None) -> int | None:
    """The message an output replies to: a stored message of this chat, else the trigger."""
    if reply_to is None:
        return deps.target.reply_to
    async with deps.db.session() as session:
        found = await session.scalar(
            select(Message.message_id).where(
                Message.chat_id == deps.chat.id,
                Message.message_id == reply_to,
                Message.deleted_at.is_(None),
            )
        )
    if found is None:
        raise Refused(f"Message #{reply_to} isn't in this chat's history.")
    return found


def reply_parameters(message_id: int | None) -> ReplyParameters | None:
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None


@toolset.tool(sequential=True)
async def send_dice(
    ctx: RunContext[RunDeps], emoji: DiceEmoji = "🎲", reply_to: MessageId | None = None
) -> str:
    """Throw an animated dice and learn the result: 🎲 1–6; 🎯 🏀 ⚽ 🎳 score hits; 🎰 a slot machine.

    Args:
        emoji: Which dice.
        reply_to: The #id to reply to (default: the current message).
    """
    deps = ctx.deps
    try:
        target = await reply_target(deps, reply_to)
        [sent] = await deliver(
            deps,
            "dice",
            "dice",
            lambda: deps.bot.send_dice(
                chat_id=deps.chat.id,
                message_thread_id=deps.target.thread_id,
                emoji=emoji,
                reply_parameters=reply_parameters(target),
            ),
        )
    except Refused as refused:
        return str(refused)
    value = sent.dice.value if sent.dice else "?"
    return f"Dice #{sent.message_id} landed on {value}."


# Telegram tags each sticker with one emoji, so one mood has several emojis (en and ru chats alike).
# Written plain (``plain_emoji``), like the sticker index; an emoji belongs to one mood at most.
MOODS: tuple[tuple[str, ...], ...] = (
    ("😂", "🤣", "😆", "😹", "😁", "😄", "😃", "😀", "😅"),  # laughing
    ("❤", "😍", "🥰", "😘", "😻", "💕", "💖", "💘", "💗", "💞", "💓", "♥", "🫶", "❤‍🔥"),  # love
    ("😢", "😭", "😿", "😞", "😔", "🥺", "☹", "🙁", "😥", "💔"),  # sad, crying
    ("😡", "🤬", "😠", "👿", "😤", "💢"),  # angry
    ("😱", "😮", "😲", "🤯", "😳", "😨", "🙀", "😯", "😦", "😧"),  # shocked
    ("👍", "👌", "✅", "🆗", "🤙", "💯"),  # thumbs up, ok
    ("🤔", "🧐", "🤨", "💭"),  # thinking
    ("🤦", "🤦‍♂", "🤦‍♀", "😬", "🙄", "😒", "😑", "😐", "🫠", "😩"),  # facepalm, cringe
    ("🎉", "🥳", "🎊", "🍾", "🥂", "🎈"),  # party
    ("😎", "🆒", "🕶", "🔥"),  # cool
    ("😴", "🥱", "💤", "😪"),  # sleepy
    ("💀", "☠", "😵", "⚰"),  # dead
    ("🤷", "🤷‍♂", "🤷‍♀"),  # shrug
    ("🙏", "🤝", "🫡"),  # thanks
    ("🤮", "🤢"),  # disgusted
    ("😈", "😏", "😼"),  # smug
    ("👋", "🙋", "🖐"),  # hi
    ("👏", "🙌"),  # applause
)  # fmt: skip
MOOD_OF = {emoji: mood for mood in MOODS for emoji in mood}
EMOJIS_SHOWN = 10  # emojis listed when nothing matches, so the model can pick one that works


async def pick_sticker(session: AsyncSession, chat_id: int, bot_id: int, emoji: str) -> str | None:
    """The emoji's sticker, else its mood's: each time the chat's most used first, then the global index."""
    exact = plain_emoji(emoji)
    mood = [e for e in MOOD_OF.get(exact, ()) if e != exact]
    for wanted in ([exact], mood):
        if not wanted:
            continue
        favorite = await session.scalar(
            select(Sticker.file_id)
            .join(MessageMedia, MessageMedia.file_unique_id == Sticker.file_unique_id)
            .where(MessageMedia.chat_id == chat_id, Sticker.emoji.in_(wanted), Sticker.bot_id == bot_id)
            .group_by(Sticker.file_id)
            .order_by(func.count().desc())
            .limit(1)
        )
        found = favorite or await session.scalar(
            select(Sticker.file_id)
            .where(Sticker.emoji.in_(wanted), Sticker.bot_id == bot_id)
            .order_by(Sticker.curated.desc(), Sticker.uses.desc())
            .limit(1)
        )
        if found:
            return found
    return None


async def sticker_emojis(session: AsyncSession, chat_id: int, bot_id: int) -> list[str]:
    """Emojis there are stickers for: this chat's most used first, topped up from the global index."""
    used = func.count()
    chat = await session.scalars(
        select(Sticker.emoji)
        .join(MessageMedia, MessageMedia.file_unique_id == Sticker.file_unique_id)
        .where(MessageMedia.chat_id == chat_id, Sticker.bot_id == bot_id, Sticker.emoji.is_not(None))
        .group_by(Sticker.emoji)
        .order_by(used.desc(), Sticker.emoji)
        .limit(EMOJIS_SHOWN)
    )
    index = await session.scalars(
        select(Sticker.emoji)
        .where(Sticker.bot_id == bot_id, Sticker.emoji.is_not(None))
        .group_by(Sticker.emoji)
        .order_by(func.bool_or(Sticker.curated).desc(), func.sum(Sticker.uses).desc(), Sticker.emoji)
        .limit(EMOJIS_SHOWN)
    )
    shown = dict.fromkeys(e for e in (*chat, *index) if e)  # ordered, unique
    return list(shown)[:EMOJIS_SHOWN]


@toolset.tool(sequential=True)
async def send_sticker(ctx: RunContext[RunDeps], emoji: str, reply_to: MessageId | None = None) -> str:
    """Send a sticker for an emoji's mood (😂 finds 🤣 stickers too), this chat's favourites first.

    Args:
        emoji: The mood as one emoji, e.g. 😂 👍 😢 🔥.
        reply_to: The #id to reply to (default: the current message).
    """
    deps = ctx.deps
    try:
        check_budget(deps)
        target = await reply_target(deps, reply_to)
    except Refused as refused:
        return str(refused)
    async with deps.db.session() as session:
        file_id = await pick_sticker(session, deps.chat.id, deps.bot.id, emoji)
        known = [] if file_id else await sticker_emojis(session, deps.chat.id, deps.bot.id)
    if file_id is None and known:
        return (
            f"No sticker for {emoji}. Stickers I have: {' '.join(known)}; use one of these or answer in text."
        )
    if file_id is None:
        return f"I don't know a sticker for {emoji} yet; answer in text."
    try:
        [sent] = await deliver(
            deps,
            "sticker",
            "sticker",
            lambda: deps.bot.send_sticker(
                chat_id=deps.chat.id,
                message_thread_id=deps.target.thread_id,
                sticker=file_id,
                reply_parameters=reply_parameters(target),
            ),
        )
    except Refused as refused:
        return str(refused)
    return f"Sticker sent as #{sent.message_id}."


@toolset.tool(sequential=True)
async def react(ctx: RunContext[RunDeps], emoji: str, message_id: MessageId | None = None) -> str:
    """React to a message; takes no output slot. A reaction alone can be the whole answer (then end with ".").

    Args:
        emoji: A standard Telegram reaction: 👍 🔥 😁 🤔 🤯 🎉 💩 🤡 🗿 👀 ❤ and the like.
        message_id: The #id to react to (default: the current message).
    """
    deps = ctx.deps
    emoji = plain_emoji(emoji)  # "❤️" is Telegram's "❤"
    if emoji not in REACTIONS:
        return "Not a reaction Telegram allows; pick a standard one like 👍 🔥 😁 🤔 🎉."
    try:
        target = await reply_target(deps, message_id)
    except Refused as refused:
        return str(refused)
    if target is None:
        return "Nothing to react to."
    try:
        await deps.bot.set_message_reaction(deps.chat.id, target, reaction=[ReactionTypeEmoji(emoji=emoji)])
    except AMBIGUOUS:
        return unconfirmed("reaction")
    except TelegramAPIError as error:
        return f"Telegram refused the reaction ({type(error).__name__}); reactions may be restricted here."
    deps.reacted.add(target)
    return f"Reacted {emoji} to #{target}."


@ops.tool
async def message_action(
    ctx: RunContext[RunDeps], action: Literal["edit", "delete", "pin"], message_id: MessageId, text: str = ""
) -> str:
    """Edit or delete one of your own messages, or pin a message (admins' asks; the tool checks).

    Args:
        action: edit (your text message, rewritten as `text`), delete (yours) or pin (any message).
        message_id: The message's #id.
        text: For edit: the full new text (Markdown).
    """
    deps = ctx.deps
    match action:
        case "edit":
            return await _edit(deps, message_id, text)
        case "delete":
            return await _delete(deps, message_id)
        case "pin":
            return await _pin(deps, message_id)


async def _own_message(deps: RunDeps, message_id: int) -> Message | None:
    async with deps.db.session() as session:
        row = await session.get(Message, (deps.chat.id, message_id))
    return row if row is not None and row.is_derp and row.deleted_at is None else None


async def _edit(deps: RunDeps, message_id: int, new_text: str) -> str:
    row = await _own_message(deps, message_id)
    if row is None or row.kind != "text":
        return "I can only edit my own text messages that are still stored."
    text = sanitize_untrusted(new_text)[:30000]
    if not text.strip():
        return "Give the full new text to edit it to."
    async with deps.db.session() as session:  # "#id" references open the message, as in answers
        links = await message_links(session, deps.chat, text)
    try:
        await deps.bot.edit_message_text(
            chat_id=deps.chat.id, message_id=message_id, rich_message=rich(text, links)
        )
    except TelegramAPIError as error:
        return f"Telegram refused the edit ({type(error).__name__})."
    async with deps.db.transaction() as session:
        stored = await session.get(Message, (deps.chat.id, message_id))
        if stored is not None:
            stored.text = text
    return f"Edited #{message_id}."


async def _delete(deps: RunDeps, message_id: int) -> str:
    if await _own_message(deps, message_id) is None:
        return "I can only delete my own messages that are still stored."
    try:
        await deps.bot.delete_message(deps.chat.id, message_id)
    except TelegramAPIError as error:
        return f"Telegram refused the deletion ({type(error).__name__})."
    async with deps.db.transaction() as session:
        stored = await session.get(Message, (deps.chat.id, message_id))
        if stored is not None:
            stored.deleted_at = datetime.now(UTC)
            stored.text = None
            await mark_stale(session, deps.chat.id, message_id)
    return f"Deleted #{message_id}."


async def _pin(deps: RunDeps, message_id: int) -> str:
    if deps.chat.type != "private" and not await is_admin(deps.bot, deps.chat, deps.user_id):
        return "Only chat admins can ask me to pin."
    try:
        target = await reply_target(deps, message_id)
    except Refused as refused:
        return str(refused)
    if target is None:
        return "Nothing to pin."
    try:
        await deps.bot.pin_chat_message(deps.chat.id, target, disable_notification=True)
    except TelegramAPIError as error:
        return f"Telegram refused ({type(error).__name__}); I probably lack the pin right here."
    return f"Pinned #{target}."


family = Family("telegram", toolset, tiers=ALL_TIERS)
ops_family = Family("telegram_ops", ops, deferred=True)
