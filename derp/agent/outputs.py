"""Visible outputs tools send: one path for the slot budget, the send and the record.

Tools may send up to ``visible_outputs - 1`` items so the final text answer
always has a slot. ``deliver`` checks that budget, performs the send and
records what Telegram accepted (``deps.delivered`` with its ``run_outputs``
kind, and the message as Derp's history unless it is a button card). Counted
outputs are what the runtime's charge rule and its "." finish read, so every
tool send goes through here.

A send Telegram didn't confirm (``telegram.policy.AMBIGUOUS``) may have
arrived: it raises ``Unconfirmed`` — never resend, neither recorded nor
charged. Any other refusal raises ``Refused``. Both carry text for the model.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiogram.types import Message as TgMessage

from derp.agent.deps import RunDeps
from derp.history.capture import record_outgoing
from derp.telegram.policy import AMBIGUOUS
from derp.telegram.sender import PartialDeliveryError, Target

Kind = Literal["image", "poll", "sticker", "dice", "invoice", "other"]  # run_outputs kinds tools produce
Sendable = Literal["photo", "video", "animation", "video_note", "voice", "document", "sticker"]
LIMIT = "Not sent: output limit for this turn reached; answer in text instead."


class Refused(Exception):  # noqa: N818 - a message for the model, not an error
    pass


class Unconfirmed(Refused):
    """Telegram didn't confirm the send: it may have arrived."""


def unconfirmed(what: str) -> str:
    """The tool result for an ambiguous send: it may have arrived, so it must never be repeated."""
    return f"Telegram didn't confirm the {what}; it may have arrived. Don't send it again."


def check_budget(deps: RunDeps, *, frees: int = 0) -> None:
    """Refuse when no output slot is left; ``frees`` slots are given back by the call (a kept result it uses)."""
    if deps.tool_outputs_left + frees <= 0:
        raise Refused(LIMIT)


async def record(deps: RunDeps, sent: Sequence[TgMessage], kind: Kind, *, history: bool = True) -> None:
    """Count accepted messages as the run's outputs; ``history`` also stores them as Derp's messages."""
    deps.delivered.extend((message.message_id, kind) for message in sent)
    if history and sent:
        async with deps.db.transaction() as session:
            await record_outgoing(session, sent, deps.bot.id)


async def deliver(
    deps: RunDeps,
    kind: Kind,
    what: str,
    send: Callable[[], Awaitable[TgMessage | list[TgMessage]]],
    *,
    history: bool = True,
    budget: bool = True,
) -> list[TgMessage]:
    """Send one output and record it; raises ``Refused`` / ``Unconfirmed`` with the model's text.

    ``history=False`` is for button cards (confirmations, approvals): outputs, but UI copy rather
    than conversation. ``budget=False`` skips the slot check for items already counted.
    """
    if budget:
        check_budget(deps)
    try:
        result = await send()
    except AMBIGUOUS:
        raise Unconfirmed(unconfirmed(what)) from None
    except PartialDeliveryError as partial:  # what Telegram accepted still counts
        await record(deps, partial.sent, kind, history=history)
        raise Unconfirmed(f"Telegram took only part of the {what}. Don't send it again.") from None
    except TelegramAPIError as error:
        raise Refused(f"Telegram refused the {what} ({type(error).__name__}).") from None
    sent = [result] if isinstance(result, TgMessage) else result
    await record(deps, sent, kind, history=history)
    return sent


def output_kind(sendable: Sendable) -> Kind:
    return "image" if sendable in ("photo", "document") else "sticker" if sendable == "sticker" else "other"


async def send_media(bot: Bot, target: Target, kind: Sendable, data: bytes, filename: str) -> TgMessage:
    """Upload media bytes as the reply to the run's trigger."""
    media = BufferedInputFile(data, filename=filename)
    chat, thread, reply = target.chat_id, target.thread_id, target.reply_parameters
    match kind:
        case "photo":
            return await bot.send_photo(chat, media, message_thread_id=thread, reply_parameters=reply)
        case "video":
            return await bot.send_video(
                chat, media, message_thread_id=thread, reply_parameters=reply, supports_streaming=True
            )
        case "animation":
            return await bot.send_animation(chat, media, message_thread_id=thread, reply_parameters=reply)
        case "video_note":
            return await bot.send_video_note(chat, media, message_thread_id=thread, reply_parameters=reply)
        case "voice":
            return await bot.send_voice(chat, media, message_thread_id=thread, reply_parameters=reply)
        case "document":
            return await bot.send_document(chat, media, message_thread_id=thread, reply_parameters=reply)
        case "sticker":
            return await bot.send_sticker(chat, media, message_thread_id=thread, reply_parameters=reply)
