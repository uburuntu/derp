"""Typed questions to the decision pin before a run, answered with probabilities.

Up to three questions go out in one call, only those that apply:

- ``addressed``: a name-only trigger in a group (``TriggerKind.NAME``) runs only when it speaks to Derp —
  "derp, what's 17×23?" does, "classic derp moment" or "derp drew a horse yesterday" don't.
- ``images``: images only the fallback rules attach (``media.run_media``: the asker's photo just before, the
  image behind an answered run) are attached only when answering needs them. Media in the trigger or the
  message it replies to is never gated.
- ``effort``: a paid turn reasons harder (``chat_settings(effort=…)``) only when the answer confidently takes
  it. It rides along with any call; on its own it is asked only for paid turns.

The state is short on purpose (accuracy drops with long or irrelevant state): the last few transcript lines,
the lines carrying fallback images and the new message, rendered as in the window. Every call is ledgered as
creator-funded silent work (``kind='decision'``, no run: it comes before one). A timeout, an error or an
unreadable answer decides as if nothing was asked: run, attach, the pin's effort.
"""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any

from aiogram.types import Message as TgMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.media import Referenced
from derp.config import Decisions
from derp.db.engine import Database
from derp.history.capture import unstored_view
from derp.history.models import Message, MessageMedia
from derp.history.render import MessageView, load_views, render_line
from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.ledger import record_usage, simple_usage
from derp.inference.pins import PinStore
from derp.inference.roles import Effort, ModelRole
from derp.ops.observability import record_decision

type Json = dict[str, Any]

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
        "looking at the photo in `images_from`?"
    ),
    "criteria": {
        "true": "The message is about the photo: it asks what is in it, to read, rate, judge or edit it, "
        "or refers to it as “this” or “it”.",
        "false": "The message is about something else and can be answered without seeing the photo.",
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
        paid: bool = False,
    ) -> Verdict:
        """Ask what applies: ``named`` (a name-only trigger), ``images`` the run would attach, ``paid`` turns."""
        fallback = images.rows if images is not None and images.fallback else []
        questions: Json = {}
        if named:
            questions["addressed"] = ADDRESSED
        if fallback:
            questions["images"] = IMAGES
        if questions or paid:
            questions["effort"] = EFFORT
        if not self.settings.enabled or not questions:
            return Verdict()
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else None
        async with self.db.session() as session:
            state = await decision_state(session, message, bot_id, tz, fallback)
        pin = await self.pins.get(ModelRole.DECISION)
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                decided = await self.api.decide(pin.model, state, questions, zdr=pin.zdr)
        except Exception as error:  # noqa: BLE001 - decisions fail open: the run goes ahead as before
            record_decision(chat_id, _failure(error), _ms(started))
            return Verdict()
        async with self.db.transaction() as session:
            record_usage(
                session,
                simple_usage(decided.model, decided.cost_usd, input_tokens=decided.input_tokens),
                kind="decision", funding="creator", role=ModelRole.DECISION.value, chat_id=chat_id, user_id=user_id,
            )  # fmt: skip
        try:
            verdict, seen = self.read(decided.answers)
        except (KeyError, TypeError, ValueError) as error:
            record_decision(chat_id, _failure(error), _ms(started))
            return Verdict()
        record_decision(chat_id, "answered", _ms(started), **seen)
        return verdict

    def read(self, answers: Json) -> tuple[Verdict, dict[str, float | str]]:
        """The verdict, and the probabilities behind it for telemetry."""
        seen: dict[str, float | str] = {}
        verdict = Verdict()
        if "addressed" in answers:
            seen["addressed"] = yes = float(answers["addressed"]["noul"])
            verdict = Verdict(addressed=yes >= self.settings.addressed)
        if "images" in answers:
            seen["images"] = yes = float(answers["images"]["noul"])
            verdict = Verdict(verdict.addressed, images=yes >= self.settings.images)
        if "effort" in answers:
            levels = {int(level): float(p) for level, p in answers["effort"]["probabilities"].items()}
            deep = levels.get(2, 0.0)
            some = levels.get(1, 0.0) + deep
            effort: Effort | None = (
                "high" if deep >= self.settings.effort else "medium" if some >= self.settings.effort else None
            )
            seen |= {"effort_some": round(some, 2), "effort_deep": round(deep, 2), "effort": effort or "pin"}
            verdict = Verdict(verdict.addressed, verdict.images, effort)
        return verdict, seen


async def decision_state(
    session: AsyncSession, message: TgMessage, bot_id: int, tz: tzinfo, fallback: Sequence[MessageMedia]
) -> Json:
    """The new message, the transcript lines just before it in its topic and those carrying fallback images."""
    chat_id = message.chat.id
    row = await session.get(Message, (chat_id, message.message_id)) if message.message_id else None
    previous: list[Message] = []
    if row is not None:
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
    views = await load_views(session, [*previous, *carriers, *([row] if row is not None else [])])
    current = views.pop() if row is not None else unstored_view(message, bot_id)

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
