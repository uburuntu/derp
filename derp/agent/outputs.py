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

Chains ("draw a cat, cut it out, make it a sticker") post only the end
result. Media tools take ``final`` (default true: post as always); with
``final=False`` the result is *kept* on the run as ``r1``, ``r2``… and the next
tool takes it as ``from_result``. Opting in keeps every single-step answer
exactly as before (posted at once, its message id and any refusal reported
back), and a model that forgets the flag only gets the old clutter. Whatever
was kept and never used is posted when the run ends (``flush``, before the
text answer), so nothing asked for is lost; a used result is never posted. A
kept result holds an output slot until a tool uses it, then hands it on.

Money travels with the result (``Pending``): the image's provider call and
heavy-effect fees are ledgered against the run only when a result carrying
them reaches Telegram — the result itself or one derived from it. A result
that never arrives has its images ledgered as ``creator`` (absorbed) and its
fees dropped; an unconfirmed send counts as never arrived, and uses up its
source. When a derived result fails to send, its source stays kept and is
posted at the end. Results needing an approval aren't kept (the approved
image comes later, in its own run).
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiogram.types import Message as TgMessage

from derp.agent.deps import RunDeps
from derp.agent.images import record_image
from derp.history.capture import record_outgoing
from derp.inference.api import GeneratedImage
from derp.money.funding import Funding
from derp.money.models import RunFee
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


def check_budget(deps: RunDeps, *, using: str | None = None) -> None:
    """Refuse when no output slot is left; a kept result the call ``using`` hands its slot on."""
    kept = deps.kept.get(using) if using else None
    if deps.tool_outputs_left + (kept is not None and not kept.used) <= 0:
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


@dataclass(slots=True)
class Pending:
    """Money a result carries until it reaches Telegram."""

    images: list[GeneratedImage] = field(default_factory=list[GeneratedImage])  # provider calls
    fees: list[str] = field(default_factory=list[str])  # heavy effects, as ``run_fees`` items

    def take(self, other: Pending) -> None:
        """Carry ``other``'s money from now on (it is moved, never counted twice)."""
        self.images += other.images
        self.fees += other.fees
        other.images, other.fees = [], []


@dataclass(slots=True)
class Made:
    """A tool's media result, not yet posted."""

    sendable: Sendable
    data: bytes
    filename: str
    what: str  # for the model: "meme", "sticker", "image"…
    pending: Pending = field(default_factory=Pending)


@dataclass(slots=True)
class Kept:
    """A result kept for the next tool (``final=False``)."""

    ref: str
    made: Made
    used: bool = False


def pending_cost(deps: RunDeps) -> tuple[Decimal, int]:
    """Provider USD and fees (count) carried by the run's kept results, not yet ledgered."""
    usd = sum((i.cost_usd for k in deps.kept.values() for i in k.made.pending.images), Decimal(0))
    return usd, sum(len(k.made.pending.fees) for k in deps.kept.values())


async def settle(deps: RunDeps, pending: Pending, *, delivered: bool) -> None:
    """Ledger a result's money: against the run once delivered, else images absorbed and fees dropped."""
    if not pending.images and not pending.fees:
        return
    async with deps.db.transaction() as session:
        for image in pending.images:
            record_image(
                session, image, deps.chat.id, deps.user_id,
                funding=deps.funding.value if delivered else "creator", run_id=deps.run_id if delivered else None,
            )  # fmt: skip
        for item in pending.fees if delivered else ():
            session.add(
                RunFee(
                    run_id=deps.run_id,
                    chat_id=deps.chat.id,
                    user_id=deps.user_id,
                    item=item,
                    funding=deps.funding.value,
                    credits=deps.settings.effect_credits if deps.funding is Funding.PAID else 0,
                )
            )
    pending.images, pending.fees = [], []


def _send(deps: RunDeps, made: Made) -> Callable[[], Awaitable[TgMessage]]:
    return lambda: send_media(deps.bot, deps.target, made.sendable, made.data, made.filename)


async def emit(deps: RunDeps, made: Made, *, final: bool, source: Kept | None = None) -> str:
    """Post a tool's media result, or keep it for the next tool; returns the text for the model.

    ``source`` is the kept result it was made from: used up (its money moving on) once this
    result is kept, posted or possibly posted; left kept if this one was refused.
    """
    check_budget(deps, using=source.ref if source else None)
    if not final:
        if source is not None:
            source.used = True
            made.pending.take(source.made.pending)
        ref = f"r{len(deps.kept) + 1}"
        deps.kept[ref] = Kept(ref, made)
        return (
            f'Kept it as result "{ref}" without posting it: pass from_result="{ref}" to the next tool. '
            "Anything kept and not used is posted when you finish."
        )
    try:
        [sent] = await deliver(deps, output_kind(made.sendable), made.what, _send(deps, made), budget=False)
    except Unconfirmed as unsure:  # it may have arrived: never posted again, its money absorbed
        if source is not None:
            source.used = True
            made.pending.take(source.made.pending)
        await settle(deps, made.pending, delivered=False)
        return str(unsure)
    except Refused as refused:
        await settle(deps, made.pending, delivered=False)
        kept = f' Result "{source.ref}" is still kept and gets posted when you finish.' if source else ""
        return f"{refused} Nothing was charged for it.{kept}"
    except BaseException:  # cancelled mid-send: absorbed (its source, still kept, follows the run)
        await settle(deps, made.pending, delivered=False)
        raise
    if source is not None:
        source.used = True
        made.pending.take(source.made.pending)
    await settle(deps, made.pending, delivered=True)
    return f'Sent it as #{sent.message_id}. Add at most a short line, or end with just ".".'


async def flush(deps: RunDeps) -> None:
    """Post the kept results no tool used, in order (they hold their slots already)."""
    for kept in deps.kept.values():
        if kept.used:
            continue
        kept.used = True
        made = kept.made
        try:
            await deliver(deps, output_kind(made.sendable), made.what, _send(deps, made), budget=False)
        except Refused:
            await settle(deps, made.pending, delivered=False)
        else:
            await settle(deps, made.pending, delivered=True)


async def absorb_kept(deps: RunDeps) -> None:
    """A run that died before ``flush``: whatever its kept results carry is absorbed, never charged."""
    for kept in deps.kept.values():
        await settle(deps, kept.made.pending, delivered=False)
