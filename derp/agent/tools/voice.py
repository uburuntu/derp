"""Voice messages: Derp acts a short line out (yelling, whispering, singing, accents, sound effects).

The model writes the performance with inline audio tags and picks one of a few
stock voices; the speech pin (``ModelRole.SPEECH``) voices it, the shared
worker pool turns the MP3 into OGG/Opus, and it goes out through
``derp.agent.outputs`` as a voice message whose script is stored as its
transcript. ``speak`` is one of the effects tools (registered in ``derp.agent.tools``).

A line is priced per character (``rates``: the pin's price snapshot, list
prices as the fallback) before it is made. The provider call travels with the
result and is ledgered against the run only once Telegram accepted it (or a
result made from it); otherwise it is absorbed. A call that timed out or was
stopped may still be billed, so it is ledgered at its estimate on Derp and
never followed by a second paid call on a fallback model. Paid runs pay its
characters; sponsored runs pay them from the day's allowance and count it as
one of the day's heavy effects.
"""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic_ai import RunContext

from derp.agent.deps import RunDeps
from derp.agent.outputs import Made, Pending, Refused, check_budget, emit, settle
from derp.agent.tools.effects import heavy_allowed
from derp.history.capture import Spoken
from derp.inference.api import OpenRouterError, Speech
from derp.inference.ledger import record_usage, simple_usage
from derp.inference.pins import snapshot_prices
from derp.inference.roles import ModelRole, RolePin
from derp.money.funding import Funding
from derp.tools import effects
from derp.tools.workers import EffectError

log = logging.getLogger("derp.voice")
# Stock voices that act well in Russian and English; the tool's docstring describes each by vibe.
type Voice = Literal["callum", "harry", "bill", "charlie", "jessica", "lily"]


@dataclass(frozen=True, slots=True)
class Rates:
    """The speech pin and USD per 1M characters for each of its models."""

    pin: RolePin
    usd_per_m: dict[str, float]

    def usd(self, model: str, characters: int) -> Decimal:
        return Decimal(str(self.usd_per_m[model] * characters / 1_000_000)).quantize(Decimal("0.00000001"))

    def most(self, characters: int) -> Decimal:
        """What a line costs at most: on the dearest model that may voice it (a fallback may)."""
        return max(self.usd(model, characters) for model in self.usd_per_m)


async def rates(deps: RunDeps) -> Rates:
    """The primary at its price snapshot (OpenRouter's price as the scout last saw it), the other models and
    a primary not yet snapshotted at the list prices in ``settings.speech``."""
    speech = deps.settings.speech
    pin = await deps.pins.get(ModelRole.SPEECH)
    async with deps.db.session() as session:
        primary, _ = await snapshot_prices(session, ModelRole.SPEECH, (speech.list_price(pin.model), 0.0))
    return Rates(pin, {m: primary if m == pin.model else speech.list_price(m) for m in pin.models})


async def _absorb(deps: RunDeps, model: str, cost: Decimal, characters: int) -> None:
    """A line that may still be billed with nothing to show for it (timed out, stopped): its estimate, on Derp."""
    async with deps.db.transaction() as session:
        record_usage(
            session, simple_usage(model, cost), kind="speech", funding="creator", role=ModelRole.SPEECH.value,
            chat_id=deps.chat.id, user_id=deps.user_id, units=characters,
        )  # fmt: skip


async def _perform(deps: RunDeps, prices: Rates, script: str, voice: str) -> Speech:
    """Voice the line on the pin's models in turn; a refused call moves on, one that may have been made
    (status 0: timed out or cut off; or stopped) is ledgered at its estimate and ends it."""
    pin = prices.pin
    error = OpenRouterError(503, "no speech model pinned")
    for model in pin.models:
        try:
            return await deps.api.speak(
                model, script, voice, cost_usd=prices.usd(model, len(script)), zdr=pin.zdr
            )
        except OpenRouterError as failed:
            log.info("speech model failed: %s", failed.status)
            if failed.status == 0:
                await _absorb(deps, model, prices.usd(model, len(script)), len(script))
                raise
            error = failed
        except asyncio.CancelledError:
            await _absorb(deps, model, prices.usd(model, len(script)), len(script))
            raise
    raise error


async def speak(ctx: RunContext[RunDeps], script: str, voice: Voice = "callum", final: bool = True) -> str:
    """Act a short line out as a voice message. Only when someone asks to hear Derp: yell, whisper, sing, laugh,
    do an accent or a sound effect. Never as a plain reading of an answer.

    Write it to be performed: audio tags in square brackets go before the words they colour, e.g. [shouting],
    [whispers], [laughs harder], [sighs], [sarcastic], [crying], [excited], [sings], [strong French accent], and
    sound effects such as [applause], [explosion], [gunshot], [fart]. CAPITALS add emphasis, "..." a pause.
    Tags are acted, never read out; the words go in the chat's language.

    Args:
        script: The performance with its audio tags; a few sentences at most (tags count towards the limit).
        voice: callum: gravelly trickster (m). harry: rough and fierce, made for yelling (m). bill: grumpy old
            man (m). charlie: hyped Aussie bloke (m). jessica: bright, playful, giggly (f). lily: velvety and
            theatrical, great at whispers (f).
        final: False keeps the voice message for a next step (e.g. reverse_media) instead of posting it.
    """
    deps = ctx.deps
    text = script.strip()
    limit = deps.settings.speech.max_chars
    try:
        check_budget(deps)
        if not text:
            raise Refused("Nothing to say: write the script.")
        if len(text) > limit:
            raise Refused(
                f"Too long for a voice message ({len(text)} characters, at most {limit} with tags)."
            )
        prices = await rates(deps)
        await heavy_allowed(ctx, price=deps.settings.credits_for_usd(float(prices.most(len(text)))))
    except Refused as refused:
        return str(refused)
    try:
        speech = await _perform(deps, prices, text, voice)
    except OpenRouterError as error:
        what = "timed out" if error.status == 0 else f"failed ({error.status})"
        return f"The voice message {what}; nothing was charged for it."
    pending = Pending(calls=[speech], fees=["voice"] if deps.funding is Funding.SPONSORED else [])
    try:
        ogg = await effects.speech(deps.workers, speech.data, bitrate="48k")
    except EffectError as error:
        await settle(deps, pending, delivered=False)
        return f"The voice message failed ({error.reason}); nothing was charged for it."
    except BaseException:  # cancelled while converting: the call is absorbed, never lost
        await settle(deps, pending, delivered=False)
        raise
    made = Made("voice", ogg, "derp.ogg", "voice message", pending, spoken=Spoken(text, speech.model))
    return await emit(deps, made, final=final)
