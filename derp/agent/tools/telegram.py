"""Telegram-native outputs (polls, dice, stickers, reactions) and Derp's own-message ops.

Every output may reply to any stored message of this chat (validated here) or
to someone's latest message by name; the default is the trigger. Tools may send
up to ``visible_outputs - 1`` items so the final text answer always has a slot.
Each delivered item is recorded as a run output and in history. Output tools run
sequentially: the budget check and the send must not interleave, and outputs
keep the order the model chose.
"""

from datetime import UTC, datetime
from typing import Literal

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InputPollOption, InputRichMessage, ReactionTypeEmoji, ReplyParameters
from aiogram.types import Message as TgMessage
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import func, select

from derp.agent.deps import RunDeps
from derp.agent.toolsets import ALL_TIERS, Family
from derp.history.capture import record_outgoing
from derp.history.digests import mark_stale
from derp.history.models import Message, MessageMedia, Sticker
from derp.history.queries import resolve_members
from derp.memory.notes import is_admin
from derp.telegram.sender import sanitize_untrusted

toolset = FunctionToolset[RunDeps]()
ops = FunctionToolset[RunDeps]()
DiceEmoji = Literal["🎲", "🎯", "🏀", "⚽", "🎳", "🎰"]
REACTIONS = (
    "❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊",
    "🤡", "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋",
    "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃",
    "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷", "🤷‍♀", "😡",
)  # fmt: skip


class OutputRefused(Exception):  # noqa: N818 - a message for the model, not an error
    pass


async def reply_target(deps: RunDeps, reply_to: int | None, reply_to_person: str | None) -> int | None:
    """The message an output replies to: a stored message of this chat, someone's latest one, or the trigger."""
    if reply_to is None and not reply_to_person:
        return deps.target.reply_to
    async with deps.db.session() as session:
        if reply_to is not None:
            found = await session.scalar(
                select(Message.message_id).where(
                    Message.chat_id == deps.chat.id,
                    Message.message_id == reply_to,
                    Message.deleted_at.is_(None),
                )
            )
            if found is None:
                raise OutputRefused(f"message #{reply_to} isn't in this chat's history")
            return found
        users = await resolve_members(session, deps.chat.id, [reply_to_person or ""])
        latest = (
            await session.scalar(
                select(func.max(Message.message_id)).where(
                    Message.chat_id == deps.chat.id, Message.user_id.in_(users), Message.deleted_at.is_(None)
                )
            )
            if users
            else None
        )
        if latest is None:
            raise OutputRefused(f"I have no stored message from {reply_to_person!r} to reply to")
        return latest


def _check_budget(deps: RunDeps) -> None:
    if deps.tool_outputs_left <= 0:
        raise OutputRefused("output limit for this turn reached; answer in text instead")


async def _delivered(deps: RunDeps, sent: TgMessage, kind: str) -> None:
    deps.delivered.append((sent.message_id, kind))
    async with deps.db.transaction() as session:
        await record_outgoing(session, [sent])


def _reply(message_id: int | None) -> ReplyParameters | None:
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None


@toolset.tool(sequential=True)
async def send_poll(
    ctx: RunContext[RunDeps],
    question: str,
    options: list[str],
    correct_option: int | None = None,
    multiple_answers: bool = False,
    anonymous: bool = False,
    reply_to: int | None = None,
    reply_to_person: str | None = None,
) -> str:
    """Post a native Telegram poll, or a quiz when `correct_option` is given.

    Args:
        question: Up to 300 characters.
        options: 2–10 answer options, up to 100 characters each.
        correct_option: 0-based index of the right answer; makes it a quiz.
        multiple_answers: Allow choosing several options (not for quizzes).
        anonymous: Hide who voted (Derp then can't see the votes).
        reply_to: Message id to reply to (default: the message that invoked you).
        reply_to_person: Reply to this person's latest message instead (name or @username).
    """
    deps = ctx.deps
    try:
        _check_budget(deps)
        if not 2 <= len(options) <= 10:
            raise OutputRefused("a poll needs 2–10 options")
        if correct_option is not None and not 0 <= correct_option < len(options):
            raise OutputRefused("correct_option must point at one of the options")
        target = await reply_target(deps, reply_to, reply_to_person)
    except OutputRefused as refused:
        return f"Not sent: {refused}."
    quiz = correct_option is not None
    try:
        sent = await deps.bot.send_poll(
            chat_id=deps.chat.id,
            message_thread_id=deps.thread_id,
            question=question[:300],
            options=[InputPollOption(text=option[:100]) for option in options],
            type="quiz" if quiz else "regular",
            is_anonymous=anonymous and deps.chat.type != "private",
            allows_multiple_answers=multiple_answers and not quiz,
            correct_option_ids=[correct_option] if correct_option is not None else None,
            reply_parameters=_reply(target),
        )
    except TelegramAPIError as error:
        return f"Telegram refused the poll ({type(error).__name__})."
    await _delivered(deps, sent, "poll")
    return f"Poll posted as #{sent.message_id}."


@toolset.tool(sequential=True)
async def send_dice(
    ctx: RunContext[RunDeps],
    emoji: DiceEmoji = "🎲",
    reply_to: int | None = None,
    reply_to_person: str | None = None,
) -> str:
    """Throw an animated Telegram dice (🎲 1–6, 🎯 🏀 ⚽ 🎳 hit/miss scores, 🎰 slot machine) and learn the result.

    Args:
        emoji: Which dice.
        reply_to: Message id to reply to (default: the message that invoked you).
        reply_to_person: Reply to this person's latest message instead.
    """
    deps = ctx.deps
    try:
        _check_budget(deps)
        target = await reply_target(deps, reply_to, reply_to_person)
    except OutputRefused as refused:
        return f"Not sent: {refused}."
    try:
        sent = await deps.bot.send_dice(
            chat_id=deps.chat.id,
            message_thread_id=deps.thread_id,
            emoji=emoji,
            reply_parameters=_reply(target),
        )
    except TelegramAPIError as error:
        return f"Telegram refused the dice ({type(error).__name__})."
    await _delivered(deps, sent, "dice")
    value = sent.dice.value if sent.dice else "?"
    return f"Dice #{sent.message_id} landed on {value}."


@toolset.tool(sequential=True)
async def send_sticker(
    ctx: RunContext[RunDeps], emoji: str, reply_to: int | None = None, reply_to_person: str | None = None
) -> str:
    """Send a sticker matching an emoji, preferring stickers this chat already uses.

    Args:
        emoji: The mood as one emoji, e.g. 😂, 👍, 😢, 🔥.
        reply_to: Message id to reply to (default: the message that invoked you).
        reply_to_person: Reply to this person's latest message instead.
    """
    deps = ctx.deps
    try:
        _check_budget(deps)
        target = await reply_target(deps, reply_to, reply_to_person)
    except OutputRefused as refused:
        return f"Not sent: {refused}."
    async with deps.db.session() as session:
        favorite = await session.scalar(
            select(Sticker.file_id)
            .join(MessageMedia, MessageMedia.file_unique_id == Sticker.file_unique_id)
            .where(MessageMedia.chat_id == deps.chat.id, Sticker.emoji == emoji)
            .group_by(Sticker.file_id)
            .order_by(func.count().desc())
            .limit(1)
        )
        file_id = favorite or await session.scalar(
            select(Sticker.file_id)
            .where(Sticker.emoji == emoji)
            .order_by(Sticker.curated.desc(), Sticker.uses.desc())
            .limit(1)
        )
    if file_id is None:
        return f"I don't know a sticker for {emoji} yet; try another emoji or answer in text."
    try:
        sent = await deps.bot.send_sticker(
            chat_id=deps.chat.id,
            message_thread_id=deps.thread_id,
            sticker=file_id,
            reply_parameters=_reply(target),
        )
    except TelegramAPIError as error:
        return f"Telegram refused the sticker ({type(error).__name__})."
    await _delivered(deps, sent, "sticker")
    return f"Sticker sent as #{sent.message_id}."


@toolset.tool(sequential=True)
async def react(ctx: RunContext[RunDeps], emoji: str, message_id: int | None = None) -> str:
    """Put a reaction on a message (default: the message that invoked you). Doesn't count as an answer.

    Args:
        emoji: One of Telegram's standard reaction emoji (e.g. 👍 🔥 😁 🤔 🤯 🎉 💩 🤡 🗿 👀 ❤).
        message_id: Message id in this chat.
    """
    deps = ctx.deps
    if emoji not in REACTIONS:
        return "Not a reaction Telegram allows; pick a standard one like 👍 🔥 😁 🤔 🎉."
    try:
        target = await reply_target(deps, message_id, None)
    except OutputRefused as refused:
        return f"Not reacted: {refused}."
    if target is None:
        return "Nothing to react to."
    try:
        await deps.bot.set_message_reaction(deps.chat.id, target, reaction=[ReactionTypeEmoji(emoji=emoji)])
    except TelegramAPIError as error:
        return f"Telegram refused the reaction ({type(error).__name__}); reactions may be restricted here."
    deps.reacted.add(target)
    return f"Reacted {emoji} to #{target}."


async def _own_message(deps: RunDeps, message_id: int) -> Message | None:
    async with deps.db.session() as session:
        row = await session.get(Message, (deps.chat.id, message_id))
    return row if row is not None and row.is_derp and row.deleted_at is None else None


@ops.tool
async def edit_own_message(ctx: RunContext[RunDeps], message_id: int, new_text: str) -> str:
    """Replace the text of one of Derp's own earlier text messages in this chat.

    Args:
        message_id: The id of Derp's message.
        new_text: The full new text (Markdown).
    """
    deps = ctx.deps
    row = await _own_message(deps, message_id)
    if row is None or row.kind != "text":
        return "I can only edit my own text messages that are still stored."
    try:
        await deps.bot.edit_message_text(
            chat_id=deps.chat.id,
            message_id=message_id,
            rich_message=InputRichMessage(markdown=sanitize_untrusted(new_text)[:30000]),
        )
    except TelegramAPIError as error:
        return f"Telegram refused the edit ({type(error).__name__})."
    async with deps.db.transaction() as session:
        stored = await session.get(Message, (deps.chat.id, message_id))
        if stored is not None:
            stored.text = new_text
    return f"Edited #{message_id}."


@ops.tool
async def delete_own_message(ctx: RunContext[RunDeps], message_id: int) -> str:
    """Delete one of Derp's own messages in this chat.

    Args:
        message_id: The id of Derp's message.
    """
    deps = ctx.deps
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


@ops.tool
async def pin_message(ctx: RunContext[RunDeps], message_id: int) -> str:
    """Pin a message in this chat. Needs Derp to be an admin with pin rights and the asker to be an admin.

    Args:
        message_id: The message to pin.
    """
    deps = ctx.deps
    if deps.chat.type != "private" and not await is_admin(deps.bot, deps.chat, deps.user_id):
        return "Only chat admins can ask me to pin."
    try:
        target = await reply_target(deps, message_id, None)
    except OutputRefused as refused:
        return f"Not pinned: {refused}."
    if target is None:
        return "Nothing to pin."
    try:
        await deps.bot.pin_chat_message(deps.chat.id, target, disable_notification=True)
    except TelegramAPIError as error:
        return f"Telegram refused ({type(error).__name__}); I probably lack the pin right here."
    return f"Pinned #{target}."


family = Family("telegram", toolset, tiers=ALL_TIERS)
ops_family = Family("telegram_ops", ops, deferred=True)
