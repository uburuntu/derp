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
result. Media tools take ``final`` (default true: post it); with
``final=False`` the result is *kept* on the run as ``r1``, ``r2``… and the next
tool takes it as ``from_result``. Keeping is opt-in so a single step stays
immediate (posted at once, its message id or refusal reported back) and a
model that skips the flag still answers, every step posted. Whatever was kept
and never used is posted when the run ends (``flush``, before the text
answer), so nothing asked for is lost; a used result is never posted. A kept
result holds an output slot until a tool uses it, then hands it on.

Money travels with the result (``Pending``): its provider calls (an image, a
voice line) and heavy-effect fees are ledgered against the run only when a
result carrying them reaches Telegram — the result itself or one derived from
it. A result that never arrives has its calls ledgered as ``creator``
(absorbed) and its fees dropped; an unconfirmed send counts as never arrived, and uses up its
source. When a derived result fails to send, its source stays kept and is
posted at the end. An image needing approval pauses the run instead; Make
it resumes the run from that call, so the chain carries on there.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiogram.types import Message as TgMessage
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse

from derp.agent.deps import RunDeps
from derp.agent.runs import totals
from derp.history.capture import Spoken, record_outgoing
from derp.inference.ledger import Billed, record_made
from derp.money.funding import Funding, run_cost, run_fees
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


async def record(
    deps: RunDeps,
    sent: Sequence[TgMessage],
    kind: Kind,
    *,
    history: bool = True,
    spoken: Spoken | None = None,
) -> None:
    """Count accepted messages as the run's outputs; ``history`` also stores them as Derp's messages.

    ``spoken`` is the script of a voice message Derp performed: stored as its transcript.
    """
    deps.delivered.extend((message.message_id, kind) for message in sent)
    if history and sent:
        async with deps.db.transaction() as session:
            await record_outgoing(session, sent, deps.bot.id, spoken=spoken)


async def deliver(
    deps: RunDeps,
    kind: Kind,
    what: str,
    send: Callable[[], Awaitable[TgMessage | list[TgMessage]]],
    *,
    history: bool = True,
    budget: bool = True,
    spoken: Spoken | None = None,
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
        await record(deps, partial.sent, kind, history=history, spoken=spoken)
        raise Unconfirmed(f"Telegram took only part of the {what}. Don't send it again.") from None
    except TelegramAPIError as error:
        raise Refused(f"Telegram refused the {what} ({type(error).__name__}).") from None
    sent = [result] if isinstance(result, TgMessage) else result
    await record(deps, sent, kind, history=history, spoken=spoken)
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

    calls: list[Billed] = field(default_factory=list[Billed])  # provider calls: images, voice lines
    fees: list[str] = field(default_factory=list[str])  # heavy effects, as ``run_fees`` items

    def take(self, other: Pending) -> None:
        """Carry ``other``'s money from now on (it is moved, never counted twice)."""
        self.calls += other.calls
        self.fees += other.fees
        other.calls, other.fees = [], []


@dataclass(slots=True)
class Made:
    """A tool's media result, not yet posted."""

    sendable: Sendable
    data: bytes
    filename: str
    what: str  # for the model: "meme", "sticker", "image"…
    pending: Pending = field(default_factory=Pending)
    spoken: Spoken | None = None  # a voice message's script, kept as its transcript


@dataclass(slots=True)
class Kept:
    """A result kept for the next tool (``final=False``)."""

    ref: str
    made: Made
    used: bool = False


def pending_cost(deps: RunDeps) -> tuple[Decimal, int]:
    """Provider USD and fees (count) carried by the run's kept results, not yet ledgered."""
    usd = sum((c.cost_usd for k in deps.kept.values() for c in k.made.pending.calls), Decimal(0))
    return usd, sum(len(k.made.pending.fees) for k in deps.kept.values())


async def hold_left(ctx: RunContext[RunDeps], *, next_turn: bool = False) -> int | None:
    """Credits of the run's hold not yet used; None when the run holds nothing (free models: no money cap).

    Used: side calls in the ledger (images, voice, media notes, digests), the run's own model turns, its fees,
    and what its kept results carry. ``next_turn`` also sets aside one more request like the latest (at
    least a credit). The one measure of the money cap: the guard wraps up, the stream stops the run and
    web search is sized by it.
    """
    deps = ctx.deps
    if deps.funds.reservation_id is None:
        return None
    async with deps.db.session() as session:
        side = await run_cost(session, deps.run_id)
        fees = await run_fees(session, deps.run_id)
    run = ctx.messages[deps.history_start :]
    kept_usd, kept_fees = pending_cost(deps)
    paid_fees = kept_fees * deps.settings.effect_credits if deps.funding is Funding.PAID else 0
    spent = deps.settings.credits_for_usd(float(totals(run).cost_usd + side + kept_usd)) + fees + paid_fees
    latest = next((m for m in reversed(run) if isinstance(m, ModelResponse)), None)
    turn = max(deps.settings.credits_for_usd(float(totals([latest]).cost_usd)) if latest else 0, 1)
    return deps.funds.credits - spent - (turn if next_turn else 0)


async def afford(ctx: RunContext[RunDeps], price: int, *, next_turn: bool) -> None:
    """Refuse side work of ``price`` (Derpcoins) this answer's hold can't cover; ``Refused`` tells the model.

    ``next_turn``: side work the answer still has to talk about (a look, a search, an image, a voice line, an
    effect) also sets aside one more turn like the latest, so paying for it never leaves the answer unable
    to finish. Only work the payer already agreed to at this price (an approved image, whose resumed run is
    funded for it alone) goes without. Free-model runs hold nothing and are never refused here.
    """
    if price and (left := await hold_left(ctx, next_turn=next_turn)) is not None and price > left:
        raise Refused(
            f"Not enough Derpcoins left in this answer for that (about {price}). Tell them /buy gets more."
        )


async def settle(deps: RunDeps, pending: Pending, *, delivered: bool) -> None:
    """Ledger a result's money: against the run once delivered, else its calls absorbed and fees dropped."""
    if not pending.calls and not pending.fees:
        return
    async with deps.db.transaction() as session:
        for call in pending.calls:
            record_made(
                session, call, chat_id=deps.chat.id, user_id=deps.user_id,
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
    pending.calls, pending.fees = [], []


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
        [sent] = await deliver(
            deps, output_kind(made.sendable), made.what, _send(deps, made), budget=False, spoken=made.spoken
        )
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
            await deliver(
                deps,
                output_kind(made.sendable),
                made.what,
                _send(deps, made),
                budget=False,
                spoken=made.spoken,
            )
        except Refused:
            await settle(deps, made.pending, delivered=False)
        else:
            await settle(deps, made.pending, delivered=True)


async def absorb_kept(deps: RunDeps) -> None:
    """A run that died before ``flush``: whatever its kept results carry is absorbed, never charged."""
    for kept in deps.kept.values():
        await settle(deps, kept.made.pending, delivered=False)
