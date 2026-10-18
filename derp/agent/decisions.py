"""Typed questions to the decision pin before a run, answered with probabilities.

Two moments ask, each in one call with only the questions that apply:

- Capture (``history.middleware``), before a message is stored: ``addressed`` — a name-only trigger in a group
  (``TriggerKind.NAME``) is an invocation only when it speaks to Derp: "derp, what's 17×23?" does, "classic
  derp moment" or "derp drew a horse yesterday" don't.
- The run, while its window is built: ``images`` — images only the fallback rules attach (``media.run_media``:
  the asker's image just before, the image behind an answered run) are attached only when answering needs
  them; media in the trigger or the message it replies to is never gated. ``effort`` — a paid turn whose hold
  covers a deeper one reasons harder (``chat_settings(effort=…)``) only when the answer confidently takes it.

The state is short on purpose (accuracy drops with long or irrelevant state): the last few transcript lines,
the lines carrying fallback images and the new message (stored or not yet), rendered as in the window. Every
call is ledgered as creator-funded silent work (``kind='decision'``, no run: it comes before one; a timed-out
call at an estimate). Any failure (the state, the pin, the call, an unreadable answer, the ledger) decides as
if nothing was asked: invoke, attach, the pin's effort.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import tzinfo
from decimal import Decimal
from typing import Any

from aiogram.types import Message as TgMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.media import Referenced
from derp.config import Decisions
from derp.db.engine import Database
from derp.history.capture import message_values, unstored_view
from derp.history.models import Message, MessageMedia
from derp.history.render import MessageView, load_views, render_line
from derp.inference.api import Decided, OpenRouterAPI, OpenRouterError, decision_body, estimated_tokens
from derp.inference.ledger import record_usage, simple_usage
from derp.inference.pins import PinStore
from derp.inference.roles import Effort, ModelRole, RolePin
from derp.ops.observability import record_decision

type Json = dict[str, Any]

log = logging.getLogger("derp.agent")

CONTEXT_LINES = 3  # transcript lines before the new message
LINE_LIMIT = 400  # characters of one line in the state

ADDRESSED: Json = {
    "type": "noul",
    "instructions": (
        "Derp (Дерп, Дерпик, Дерпуша) is an AI bot in this group chat. Does the author of `new_message` use "
        "the name to speak to the bot, as in “Derp, …” or “what do you think, Derp?”"
    ),
    "criteria": {
        "true": "Derp is addressed in the second person: a question, request or remark directed at the bot.",
        "false": "derp is used as a word (silly, a goof, a blunder), or the bot is mentioned in the third person.",
    },
}
IMAGES: Json = {
    "type": "noul",
    "instructions": (
        "Derp is an AI bot in this group chat. `new_message` asks Derp something. Does answering it require "
        "looking at the image in `images_from`?"
    ),
    "criteria": {
        "true": "The message is about the image: it asks what is in it, to read, rate, judge or edit it, "
        "or refers to it as “this” or “it”.",
        "false": "The message is about something else and can be answered without seeing the image.",
    },
}
EFFORT: Json = {
    "type": "score",
    "instructions": "How much step-by-step reasoning does a correct answer to `new_message` take?",
    "criteria": [
        "None: chat, banter, thanks, a joke, a known fact, a familiar explanation or a one-step calculation",
        "Some: a calculation or comparison with several steps, or advice that weighs numbers",
        "A lot: a proof, code, a logic puzzle or long multi-step math",
    ],
}


@dataclass(frozen=True, slots=True)
class Verdict:
    addressed: bool = True
    images: bool = True  # fallback images are attached
    effort: Effort | None = None  # a deeper reasoning effort for a paid turn; None keeps the pin's


class Decider:
    def __init__(self, db: Database, api: OpenRouterAPI, pins: PinStore, settings: Decisions) -> None:
        self.db = db
        self.api = api
        self.pins = pins
        self.settings = settings

    async def decide(
        self,
        message: TgMessage,
        *,
        bot_id: int,
        tz: tzinfo,
        named: bool = False,
        images: Referenced | None = None,
        effort: bool = False,
    ) -> Verdict:
        """Ask what applies: ``named`` (a name-only trigger), fallback ``images`` the run would attach,
        ``effort`` (a paid turn whose hold covers a deeper one). Never raises: a failure is ``Verdict()``."""
        fallback = images.rows if images is not None and images.fallback else []
        questions: Json = {}
        if named:
            questions["addressed"] = ADDRESSED
        if fallback:
            questions["images"] = IMAGES
        if effort:
            questions["effort"] = EFFORT
        if not self.settings.enabled or not questions:
            return Verdict()
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else None
        started = time.monotonic()
        state: Json | None = None
        pin: RolePin | None = None
        try:
            async with self.db.session() as session:
                state = await decision_state(session, message, bot_id, tz, fallback)
            pin = await self.pins.get(ModelRole.DECISION)
            async with asyncio.timeout(self.settings.timeout_seconds):
                decided = await self.api.decide(pin.model, state, questions, zdr=pin.zdr)
        except Exception as error:  # noqa: BLE001 - decisions fail open: the run goes ahead as before
            record_decision(chat_id, _failure(error), _ms(started))
            if isinstance(error, TimeoutError) and state is not None and pin is not None:
                # The provider may well have answered (and billed) after we stopped waiting.
                tokens = estimated_tokens(decision_body(pin.model, state, questions, zdr=pin.zdr))
                await self._ledger(Decided({}, pin.model, self._estimate(tokens), tokens), chat_id, user_id)
            return Verdict()
        await self._ledger(decided, chat_id, user_id)
        try:
            verdict, seen = self.read(decided.answers)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            record_decision(chat_id, _failure(error), _ms(started))
            return Verdict()
        record_decision(chat_id, "answered", _ms(started), **seen)
        return verdict

    def _estimate(self, tokens: int) -> Decimal:
        return (Decimal(str(self.settings.price_usd_per_m_input)) * tokens / 1_000_000).quantize(
            Decimal("0.00000001")
        )

    async def _ledger(self, decided: Decided, chat_id: int, user_id: int | None) -> None:
        """Creator-funded and outside any run; a ledger failure is logged and never blocks the run."""
        try:
            async with self.db.transaction() as session:
                record_usage(
                    session,
                    simple_usage(decided.model, decided.cost_usd, input_tokens=decided.input_tokens),
                    kind="decision", funding="creator", role=ModelRole.DECISION.value, chat_id=chat_id,
                    user_id=user_id,
                )  # fmt: skip
        except Exception as error:  # noqa: BLE001 - money telemetry for creator spend, not a reason to fail
            log.warning("decision in %s not ledgered: %s", chat_id, type(error).__name__)

    def read(self, answers: Json) -> tuple[Verdict, dict[str, float | str]]:
        """The verdict, and the probabilities behind it for telemetry."""
        seen: dict[str, float | str] = {}
        verdict = Verdict()
        if "addressed" in answers:
            seen["addressed"] = yes = float(answers["addressed"]["noul"])
            verdict = replace(verdict, addressed=yes >= self.settings.addressed)
        if "images" in answers:
            seen["images"] = yes = float(answers["images"]["noul"])
            verdict = replace(verdict, images=yes >= self.settings.images)
        if "effort" in answers:
            levels = {int(level): float(p) for level, p in answers["effort"]["probabilities"].items()}
            deep = levels.get(2, 0.0)
            some = levels.get(1, 0.0) + deep
            effort: Effort | None = (
                "high" if deep >= self.settings.effort else "medium" if some >= self.settings.effort else None
            )
            seen |= {"effort_some": round(some, 2), "effort_deep": round(deep, 2), "effort": effort or "pin"}
            verdict = replace(verdict, effort=effort)
        return verdict, seen


async def decision_state(
    session: AsyncSession, message: TgMessage, bot_id: int, tz: tzinfo, fallback: Sequence[MessageMedia]
) -> Json:
    """The new message, the transcript lines just before it in its topic and those carrying fallback images.

    The new message may not be stored yet (capture screens before it stores): it is then rendered as it would be.
    """
    chat_id = message.chat.id
    stored = await session.get(Message, (chat_id, message.message_id)) if message.message_id else None
    row = stored or Message(**message_values(message, capture="invocation"))
    previous = list(
        await session.scalars(
            select(Message)
            .where(
                Message.chat_id == chat_id,
                Message.message_id < row.message_id,
                Message.deleted_at.is_(None),
                Message.thread_id == row.thread_id
                if row.thread_id is not None
                else Message.thread_id.is_(None),
            )
            .order_by(Message.message_id.desc())
            .limit(CONTEXT_LINES)
        )
    )[::-1]
    carriers = [
        carrier
        for message_id in dict.fromkeys(media.message_id for media in fallback)
        if (carrier := await session.get(Message, (chat_id, message_id))) is not None
    ]
    views = await load_views(session, [*previous, *carriers, *([stored] if stored is not None else [])])
    current = views.pop() if stored is not None else unstored_view(message, bot_id)

    def line(view: MessageView) -> str:
        return render_line(view, tz, reactions=False)[:LINE_LIMIT]

    state: Json = {"previous_messages": [line(view) for view in views[: len(previous)]]}
    if carriers:
        state["images_from"] = [line(view) for view in views[len(previous) :]]
    state["new_message"] = line(current)
    return state


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _failure(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OpenRouterError):
        return f"error_{error.status}"
    return type(error).__name__
